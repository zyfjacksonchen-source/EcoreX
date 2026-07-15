"""Signed Core delta construction and atomic application.

A delta is a normal artifact in the *target* signed release manifest.  Its
descriptor additionally binds the exact retained base package and the exact
target Core identity.  Applying a delta never makes the patch a trust root:
the reconstructed package must still pass the target Core SHA-256 and detached
signature before it can atomically replace the transaction package.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat as stat_module
from typing import Any, Mapping
import zipfile
import zlib

from .manifest import ReleaseArtifact, ReleaseManifest
from .verification import (
    SignatureVerifier,
    sha256_file,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)


DELTA_SCHEMA_VERSION = 1
DELTA_ALGORITHM = "content-defined-copy-zlib-v1"
DELTA_CHUNK_SIZE = 64 * 1024
DELTA_MIN_CHUNK_SIZE = 16 * 1024
DELTA_MAX_CHUNK_SIZE = 128 * 1024
DELTA_DESCRIPTOR_NAME = "delta.json"
DELTA_LITERALS_NAME = "literals.bin"
MAX_DELTA_DESCRIPTOR_BYTES = 4 * 1024 * 1024
MAX_DELTA_OPERATIONS = 16_384
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_GEAR = tuple(
    int.from_bytes(
        hashlib.sha256(b"ecorex-delta-gear-v1\0" + bytes((value,))).digest()[:8],
        "big",
    )
    for value in range(256)
)


class DeltaError(RuntimeError):
    pass


class DeltaNotBeneficial(DeltaError):
    pass


@dataclass(frozen=True, slots=True)
class CoreDeltaEndpoint:
    release_id: str
    version: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str
    artifact_size_bytes: int

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.release_id):
            raise DeltaError("delta release identity is invalid")
        if not self.version or len(self.version.encode("utf-8")) > 128:
            raise DeltaError("delta version identity is invalid")
        if not _SHA256.fullmatch(self.build_digest):
            raise DeltaError("delta build identity is invalid")
        if not _SAFE_ID.fullmatch(self.artifact_id):
            raise DeltaError("delta artifact identity is invalid")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise DeltaError("delta artifact digest is invalid")
        if (
            isinstance(self.artifact_size_bytes, bool)
            or not isinstance(self.artifact_size_bytes, int)
            or self.artifact_size_bytes < 1
        ):
            raise DeltaError("delta artifact size is invalid")

    @classmethod
    def from_release(
        cls, manifest: ReleaseManifest, artifact: ReleaseArtifact
    ) -> "CoreDeltaEndpoint":
        try:
            bound = manifest.artifact(artifact.artifact_id)
        except Exception:
            raise DeltaError("delta endpoint artifact is absent from its release") from None
        if bound != artifact:
            raise DeltaError("delta endpoint artifact differs from its signed release")
        return cls(
            release_id=manifest.release_id,
            version=manifest.version,
            build_digest=manifest.build_digest,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            artifact_size_bytes=artifact.size_bytes,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoreDeltaEndpoint":
        expected = {
            "release_id",
            "version",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "artifact_size_bytes",
        }
        if set(value) != expected:
            raise DeltaError("delta endpoint shape is invalid")
        return cls(
            release_id=str(value.get("release_id") or ""),
            version=str(value.get("version") or ""),
            build_digest=str(value.get("build_digest") or ""),
            artifact_id=str(value.get("artifact_id") or ""),
            artifact_sha256=str(value.get("artifact_sha256") or ""),
            artifact_size_bytes=value.get("artifact_size_bytes"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size_bytes": self.artifact_size_bytes,
        }


def core_delta_artifact_id(
    *, platform: str, architecture: str, base_artifact_sha256: str
) -> str:
    if (
        not _SAFE_ID.fullmatch(platform)
        or not _SAFE_ID.fullmatch(architecture)
        or not _SHA256.fullmatch(base_artifact_sha256)
    ):
        raise DeltaError("delta artifact selector is invalid")
    value = f"core-delta-{platform}-{architecture}-from-{base_artifact_sha256[:16]}"
    if not _SAFE_ID.fullmatch(value):
        raise DeltaError("delta artifact ID is too long")
    return value


def core_delta_file_name(
    *,
    platform: str,
    architecture: str,
    base_artifact_sha256: str,
    target_artifact_sha256: str,
) -> str:
    if not _SHA256.fullmatch(target_artifact_sha256):
        raise DeltaError("delta target digest is invalid")
    artifact_id = core_delta_artifact_id(
        platform=platform,
        architecture=architecture,
        base_artifact_sha256=base_artifact_sha256,
    )
    return f"ecorex-{artifact_id}-to-{target_artifact_sha256[:16]}.ecdx"


def select_core_delta_artifact(
    manifest: ReleaseManifest,
    *,
    target_artifact: ReleaseArtifact,
    base_artifact: ReleaseArtifact,
) -> ReleaseArtifact | None:
    try:
        bound_target = manifest.artifact(target_artifact.artifact_id)
    except Exception:
        raise DeltaError("delta target artifact is absent from its release") from None
    if bound_target != target_artifact:
        raise DeltaError("delta target artifact differs from its signed release")
    expected_id = core_delta_artifact_id(
        platform=target_artifact.platform,
        architecture=target_artifact.architecture,
        base_artifact_sha256=base_artifact.sha256,
    )
    candidates = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == expected_id
    ]
    if not candidates:
        return None
    delta = candidates[0]
    if (
        delta.platform != target_artifact.platform
        or delta.architecture != target_artifact.architecture
        or delta.size_bytes >= target_artifact.size_bytes
    ):
        raise DeltaError("signed delta artifact identity is invalid or not beneficial")
    return delta


def create_core_delta_archive(
    *,
    base_package: Path,
    target_package: Path,
    base: CoreDeltaEndpoint,
    target: CoreDeltaEndpoint,
    destination: Path,
    chunk_size: int = DELTA_CHUNK_SIZE,
) -> Path:
    """Create a deterministic chunk-copy delta smaller than the full target."""

    if chunk_size != DELTA_CHUNK_SIZE:
        raise DeltaError("v1 delta chunk size is fixed")
    _verify_endpoint_file(base_package, base, "base")
    _verify_endpoint_file(target_package, target, "target")
    if base == target:
        raise DeltaNotBeneficial("identical Core packages do not require a delta")
    base_chunks: dict[tuple[str, int], int] = {}
    for offset, chunk in _content_defined_chunks(base_package):
        base_chunks.setdefault((hashlib.sha256(chunk).hexdigest(), len(chunk)), offset)

    operations: list[dict[str, object]] = []
    literals = bytearray()
    for target_offset, chunk in _content_defined_chunks(target_package):
        digest = hashlib.sha256(chunk).hexdigest()
        base_offset = base_chunks.get((digest, len(chunk)))
        if base_offset is not None:
            operation = {
                "kind": "copy",
                "target_offset": target_offset,
                "length": len(chunk),
                "sha256": digest,
                "base_offset": base_offset,
            }
        else:
            compressed = zlib.compress(chunk, level=9)
            literal_offset = len(literals)
            literals.extend(compressed)
            operation = {
                "kind": "literal",
                "target_offset": target_offset,
                "length": len(chunk),
                "sha256": digest,
                "literal_offset": literal_offset,
                "literal_size_bytes": len(compressed),
            }
        operations.append(operation)
        if len(operations) > MAX_DELTA_OPERATIONS:
            raise DeltaError("delta operation limit exceeded")

    descriptor = {
        "schema_version": DELTA_SCHEMA_VERSION,
        "algorithm": DELTA_ALGORITHM,
        "chunk_size": DELTA_CHUNK_SIZE,
        "base": base.to_dict(),
        "target": target.to_dict(),
        "operations": operations,
        "literals_sha256": hashlib.sha256(literals).hexdigest(),
        "literals_size_bytes": len(literals),
    }
    descriptor_bytes = _canonical_json(descriptor) + b"\n"
    if len(descriptor_bytes) > MAX_DELTA_DESCRIPTOR_BYTES:
        raise DeltaError("delta descriptor limit exceeded")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        raise DeltaError("delta destination already exists")
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            archive.writestr(_zip_info(DELTA_DESCRIPTOR_NAME), descriptor_bytes)
            archive.writestr(_zip_info(DELTA_LITERALS_NAME), bytes(literals))
        if temporary.stat().st_size >= target.artifact_size_bytes:
            raise DeltaNotBeneficial(
                "generated delta is not smaller than the signed full target"
            )
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_core_delta_archive(
    *,
    delta_path: Path,
    delta_artifact: ReleaseArtifact,
    base_package: Path,
    base_manifest: ReleaseManifest,
    base_artifact: ReleaseArtifact,
    target_path: Path,
    target_manifest: ReleaseManifest,
    target_artifact: ReleaseArtifact,
    verifier: SignatureVerifier,
) -> Path:
    """Atomically reconstruct and authenticate the exact target Core package."""

    for label, release, candidate in (
        ("base", base_manifest, base_artifact),
        ("target", target_manifest, target_artifact),
        ("delta", target_manifest, delta_artifact),
    ):
        try:
            bound = release.artifact(candidate.artifact_id)
        except Exception:
            raise DeltaError(f"{label} artifact is absent from its signed release") from None
        if bound != candidate:
            raise DeltaError(f"{label} artifact differs from its signed release")
    verify_manifest_signature(base_manifest, verifier)
    verify_manifest_signature(target_manifest, verifier)
    verify_artifact_file(base_package, base_manifest, base_artifact, verifier)
    verify_artifact_file(delta_path, target_manifest, delta_artifact, verifier)
    verify_artifact_signature(target_manifest, target_artifact, verifier)
    descriptor, literals = _read_delta(delta_path)
    expected_base = CoreDeltaEndpoint.from_release(base_manifest, base_artifact)
    expected_target = CoreDeltaEndpoint.from_release(target_manifest, target_artifact)
    if descriptor["base"] != expected_base or descriptor["target"] != expected_target:
        raise DeltaError("delta base or target identity does not match signed releases")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target_path):
        raise DeltaError("delta target path must be absent before atomic staging")
    temporary = target_path.with_name(
        f".{target_path.name}.delta-{os.getpid()}-{secrets.token_hex(8)}"
    )
    replaced = False
    try:
        with base_package.open("rb") as base_stream, temporary.open("xb") as output:
            written = 0
            for operation in descriptor["operations"]:
                length = operation["length"]
                if operation["kind"] == "copy":
                    base_stream.seek(operation["base_offset"])
                    chunk = base_stream.read(length)
                else:
                    start = operation["literal_offset"]
                    end = start + operation["literal_size_bytes"]
                    chunk = _decompress_exact(literals[start:end], length)
                if (
                    len(chunk) != length
                    or hashlib.sha256(chunk).hexdigest() != operation["sha256"]
                ):
                    raise DeltaError("delta chunk authentication failed")
                output.write(chunk)
                written += len(chunk)
                if written > target_artifact.size_bytes:
                    raise DeltaError("delta output exceeded signed target size")
            output.flush()
            os.fsync(output.fileno())
        if written != target_artifact.size_bytes:
            raise DeltaError("delta output size differs from signed target")
        verify_artifact_file(temporary, target_manifest, target_artifact, verifier)
        os.replace(temporary, target_path)
        replaced = True
        _fsync_directory(target_path.parent)
        verify_artifact_file(target_path, target_manifest, target_artifact, verifier)
        return target_path
    except Exception:
        if replaced:
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_delta(
    path: Path,
) -> tuple[dict[str, Any], bytes]:
    _require_regular(path, "delta archive")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if archive.comment:
                raise DeltaError("delta archive comment is forbidden")
            infos = archive.infolist()
            if [info.filename for info in infos] != [
                DELTA_DESCRIPTOR_NAME,
                DELTA_LITERALS_NAME,
            ]:
                raise DeltaError("delta archive member set is invalid")
            for info in infos:
                if (
                    info.flag_bits & 0x1
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.is_dir()
                    or (info.external_attr >> 16) & 0o170000 not in {0, 0o100000}
                ):
                    raise DeltaError("delta archive member is unsafe")
            descriptor_info, literals_info = infos
            if not 1 <= descriptor_info.file_size <= MAX_DELTA_DESCRIPTOR_BYTES:
                raise DeltaError("delta descriptor size is invalid")
            if literals_info.file_size > 150 * 1024 * 1024:
                raise DeltaError("delta literal payload is too large")
            descriptor_bytes = archive.read(descriptor_info)
            literals = archive.read(literals_info)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise DeltaError("delta archive is unreadable") from None
    descriptor = _parse_descriptor(descriptor_bytes, literals)
    return descriptor, literals


def _parse_descriptor(payload: bytes, literals: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DeltaError("delta descriptor is invalid JSON") from None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "algorithm",
        "chunk_size",
        "base",
        "target",
        "operations",
        "literals_sha256",
        "literals_size_bytes",
    }:
        raise DeltaError("delta descriptor shape is invalid")
    if (
        value.get("schema_version") != DELTA_SCHEMA_VERSION
        or value.get("algorithm") != DELTA_ALGORITHM
        or value.get("chunk_size") != DELTA_CHUNK_SIZE
        or value.get("literals_size_bytes") != len(literals)
        or value.get("literals_sha256") != hashlib.sha256(literals).hexdigest()
    ):
        raise DeltaError("delta descriptor identity is invalid")
    if not isinstance(value.get("base"), Mapping) or not isinstance(
        value.get("target"), Mapping
    ):
        raise DeltaError("delta endpoint is invalid")
    base = CoreDeltaEndpoint.from_dict(value["base"])
    target = CoreDeltaEndpoint.from_dict(value["target"])
    raw_operations = value.get("operations")
    if (
        not isinstance(raw_operations, list)
        or not 1 <= len(raw_operations) <= MAX_DELTA_OPERATIONS
    ):
        raise DeltaError("delta operation set is invalid")
    operations: list[dict[str, Any]] = []
    target_offset = 0
    literal_offset = 0
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise DeltaError("delta operation is invalid")
        kind = raw.get("kind")
        common = {"kind", "target_offset", "length", "sha256"}
        expected = common | (
            {"base_offset"}
            if kind == "copy"
            else {"literal_offset", "literal_size_bytes"}
            if kind == "literal"
            else set()
        )
        length = raw.get("length")
        if (
            kind not in {"copy", "literal"}
            or set(raw) != expected
            or raw.get("target_offset") != target_offset
            or isinstance(length, bool)
            or not isinstance(length, int)
            or not 1 <= length <= DELTA_MAX_CHUNK_SIZE
            or not isinstance(raw.get("sha256"), str)
            or _SHA256.fullmatch(raw["sha256"]) is None
        ):
            raise DeltaError("delta operation identity is invalid")
        if kind == "copy":
            base_offset = raw.get("base_offset")
            if (
                isinstance(base_offset, bool)
                or not isinstance(base_offset, int)
                or base_offset < 0
                or base_offset + length > base.artifact_size_bytes
            ):
                raise DeltaError("delta copy operation is out of bounds")
        else:
            literal_size = raw.get("literal_size_bytes")
            if (
                raw.get("literal_offset") != literal_offset
                or isinstance(literal_size, bool)
                or not isinstance(literal_size, int)
                or literal_size < 1
                or literal_offset + literal_size > len(literals)
            ):
                raise DeltaError("delta literal operation is out of bounds")
            literal_offset += literal_size
        operations.append(dict(raw))
        target_offset += length
    if target_offset != target.artifact_size_bytes or literal_offset != len(literals):
        raise DeltaError("delta operations do not exactly cover target and literals")
    canonical = _canonical_json(value) + b"\n"
    if canonical != payload:
        raise DeltaError("delta descriptor is not canonical")
    return {"base": base, "target": target, "operations": operations}


def _verify_endpoint_file(path: Path, endpoint: CoreDeltaEndpoint, label: str) -> None:
    metadata = _require_regular(path, f"{label} package")
    if (
        metadata.st_size != endpoint.artifact_size_bytes
        or sha256_file(path) != endpoint.artifact_sha256
    ):
        raise DeltaError(f"{label} package differs from its signed identity")


def _content_defined_chunks(path: Path):
    """Yield deterministic boundaries that re-synchronize after insertions."""

    with path.open("rb") as stream:
        offset = 0
        chunk = bytearray()
        rolling = 0
        while block := stream.read(1024 * 1024):
            for value in block:
                chunk.append(value)
                rolling = ((rolling << 1) + _GEAR[value]) & 0xFFFFFFFFFFFFFFFF
                length = len(chunk)
                if length >= DELTA_MIN_CHUNK_SIZE and (
                    (rolling & (DELTA_CHUNK_SIZE - 1)) == 0
                    or length >= DELTA_MAX_CHUNK_SIZE
                ):
                    yield offset, bytes(chunk)
                    offset += length
                    chunk.clear()
                    rolling = 0
        if chunk:
            yield offset, bytes(chunk)


def _require_regular(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise DeltaError(f"{label} is unavailable") from None
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
    ):
        raise DeltaError(f"{label} is not a regular file")
    return metadata


def _decompress_exact(payload: bytes, expected_size: int) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        value = decompressor.decompress(payload, expected_size + 1)
    except zlib.error:
        raise DeltaError("delta literal stream is invalid") from None
    # Never call ``flush`` on an incomplete stream. CPython's zlib flush length
    # is a buffer hint, not an output ceiling; a hostile stream can otherwise
    # expand far beyond the signed chunk bound before the final length check.
    # ``expected_size + 1`` lets us detect overflow while still requiring the
    # complete input stream to have reached EOF in this single bounded call.
    if (
        len(value) > expected_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(value) != expected_size
    ):
        raise DeltaError("delta literal stream has invalid bounds")
    return value


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (0o100600 & 0xFFFF) << 16
    info.flag_bits = 0x800
    return info


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CoreDeltaEndpoint",
    "DELTA_ALGORITHM",
    "DELTA_CHUNK_SIZE",
    "DELTA_SCHEMA_VERSION",
    "DeltaError",
    "DeltaNotBeneficial",
    "apply_core_delta_archive",
    "core_delta_artifact_id",
    "core_delta_file_name",
    "create_core_delta_archive",
    "select_core_delta_artifact",
]
