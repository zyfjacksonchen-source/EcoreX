"""Signed web-bundle manifest contract used by the product server."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from ecorex.update import SignatureEnvelope
from ecorex.update.manifest import (
    ManifestError,
    portable_path_segment_key,
    validate_portable_path_segment,
)

from .errors import BundleIntegrityError


WEB_MANIFEST_SCHEMA_VERSION = 1
MAX_WEB_MANIFEST_BYTES = 1024 * 1024
MAX_WEB_FILE_BYTES = 150 * 1024 * 1024
MAX_WEB_BUNDLE_BYTES = 150 * 1024 * 1024
MAX_WEB_FILES = 4096
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _portable_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BundleIntegrityError("web file path is missing or unsafe")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or any(
            part in {"", ".", ".."} or part.startswith(".") or ":" in part
            for part in path.parts
        )
        or path.parts[0].casefold() == "api"
    ):
        raise BundleIntegrityError(f"web file path is unsafe: {value!r}")
    try:
        for segment in path.parts:
            validate_portable_path_segment(segment, label="web file path segment")
    except ManifestError as error:
        raise BundleIntegrityError(f"web file path is not portable: {value!r}") from error
    return value


def _path_collision_key(value: str) -> str:
    return "/".join(
        portable_path_segment_key(part) for part in PurePosixPath(value).parts
    )


@dataclass(frozen=True, slots=True)
class WebFileRecord:
    path: str
    size_bytes: int
    sha256: str
    immutable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _portable_path(self.path))
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 1
            or self.size_bytes > MAX_WEB_FILE_BYTES
        ):
            raise BundleIntegrityError("web file size is outside the supported range")
        if not isinstance(self.immutable, bool):
            raise BundleIntegrityError("web file immutable must be a boolean")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise BundleIntegrityError("web file SHA-256 must be 64 hexadecimal characters")
        object.__setattr__(self, "sha256", self.sha256.lower())
        if self.immutable and self.sha256[:8] not in PurePosixPath(self.path).name.casefold():
            raise BundleIntegrityError(
                f"immutable web file name must contain its SHA-256 prefix: {self.path!r}"
            )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WebFileRecord":
        expected = {"path", "size_bytes", "sha256", "immutable"}
        if set(raw) != expected:
            raise BundleIntegrityError("web file record has invalid fields")
        return cls(
            path=raw.get("path"),
            size_bytes=raw.get("size_bytes"),
            sha256=raw.get("sha256"),
            immutable=raw.get("immutable"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "immutable": self.immutable,
        }


@dataclass(frozen=True, slots=True)
class WebBundleManifest:
    schema_version: int
    release_id: str
    version: str
    build_digest: str
    bundle_sha256: str
    entrypoint: str
    files: tuple[WebFileRecord, ...]
    signature: SignatureEnvelope

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != WEB_MANIFEST_SCHEMA_VERSION
        ):
            raise BundleIntegrityError("unsupported web manifest schema version")
        if not isinstance(self.release_id, str) or not _SAFE_ID.fullmatch(self.release_id):
            raise BundleIntegrityError("web manifest release_id is unsafe")
        if not isinstance(self.version, str) or not _SEMVER.fullmatch(self.version):
            raise BundleIntegrityError("web manifest version is not SemVer")
        if not isinstance(self.build_digest, str) or not _SHA256.fullmatch(self.build_digest):
            raise BundleIntegrityError("web manifest build digest is invalid")
        if not isinstance(self.bundle_sha256, str) or not _SHA256.fullmatch(
            self.bundle_sha256
        ):
            raise BundleIntegrityError("web manifest bundle SHA-256 is invalid")
        object.__setattr__(self, "build_digest", self.build_digest.lower())
        object.__setattr__(self, "bundle_sha256", self.bundle_sha256.lower())
        object.__setattr__(self, "entrypoint", _portable_path(self.entrypoint))
        if (
            not isinstance(self.files, tuple)
            or not self.files
            or len(self.files) > MAX_WEB_FILES
            or not all(isinstance(record, WebFileRecord) for record in self.files)
        ):
            raise BundleIntegrityError("web manifest file count is invalid")
        if not isinstance(self.signature, SignatureEnvelope):
            raise BundleIntegrityError("web manifest signature envelope is invalid")
        if sum(record.size_bytes for record in self.files) > MAX_WEB_BUNDLE_BYTES:
            raise BundleIntegrityError("web bundle exceeds the 150 MiB hard limit")
        paths = [record.path for record in self.files]
        collision_keys = [_path_collision_key(path) for path in paths]
        if len(collision_keys) != len(set(collision_keys)):
            raise BundleIntegrityError("web manifest contains colliding file paths")
        if paths.count(self.entrypoint) != 1:
            raise BundleIntegrityError("web manifest entrypoint must exist exactly once")
        entrypoint = next(record for record in self.files if record.path == self.entrypoint)
        if self.entrypoint != "index.html" or entrypoint.immutable:
            raise BundleIntegrityError("web entrypoint must be non-immutable index.html")
        if any(
            record.path != self.entrypoint and not record.immutable
            for record in self.files
        ):
            raise BundleIntegrityError("all non-entrypoint web files must be immutable")
        if any(
            PurePosixPath(record.path).suffix.casefold() in {".htm", ".html"}
            and record.path != self.entrypoint
            for record in self.files
        ):
            raise BundleIntegrityError("index.html must be the only HTML file in the bundle")
        calculated = self.compute_bundle_sha256(self.files)
        if calculated != self.bundle_sha256:
            raise BundleIntegrityError(
                f"web bundle SHA-256 mismatch: expected {self.bundle_sha256}, got {calculated}"
            )

    @staticmethod
    def compute_bundle_sha256(files: Sequence[WebFileRecord]) -> str:
        canonical = "".join(
            f"{record.path}\0{record.size_bytes}\0{record.sha256}\0"
            f"{1 if record.immutable else 0}\n"
            for record in sorted(files, key=lambda value: value.path)
        ).encode("utf-8")
        return hashlib.sha256(b"ecorex-web-bundle-files-v1\n" + canonical).hexdigest()

    @classmethod
    def from_json(cls, payload: str | bytes) -> "WebBundleManifest":
        if not isinstance(payload, (str, bytes)):
            raise BundleIntegrityError("web manifest payload must be text or bytes")
        try:
            size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        except UnicodeEncodeError as error:
            raise BundleIntegrityError("web manifest is not valid UTF-8 JSON") from error
        if size > MAX_WEB_MANIFEST_BYTES:
            raise BundleIntegrityError("web manifest exceeds 1 MiB")
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BundleIntegrityError("web manifest is not valid UTF-8 JSON") from error
        if not isinstance(raw, Mapping):
            raise BundleIntegrityError("web manifest root must be an object")
        expected = {
            "schema_version",
            "release_id",
            "version",
            "build_digest",
            "bundle_sha256",
            "entrypoint",
            "files",
            "signature",
        }
        if set(raw) != expected:
            raise BundleIntegrityError("web manifest has invalid fields")
        files = raw.get("files")
        signature = raw.get("signature")
        if (
            not isinstance(files, Sequence)
            or isinstance(files, (str, bytes, bytearray))
            or not all(isinstance(item, Mapping) for item in files)
            or not isinstance(signature, Mapping)
        ):
            raise BundleIntegrityError("web manifest files or signature are invalid")
        try:
            envelope = SignatureEnvelope.from_dict(signature)
        except ValueError as error:
            raise BundleIntegrityError("web manifest signature envelope is invalid") from error
        return cls(
            schema_version=raw.get("schema_version"),
            release_id=raw.get("release_id"),
            version=raw.get("version"),
            build_digest=raw.get("build_digest"),
            bundle_sha256=raw.get("bundle_sha256"),
            entrypoint=raw.get("entrypoint"),
            files=tuple(WebFileRecord.from_dict(item) for item in files),
            signature=envelope,
        )

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "bundle_sha256": self.bundle_sha256,
            "entrypoint": self.entrypoint,
            "files": [record.to_dict() for record in self.files],
        }
        if include_signature:
            result["signature"] = self.signature.to_dict()
        return result

    def canonical_payload(self) -> bytes:
        encoded = json.dumps(
            self.to_dict(include_signature=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return b"ecorex-web-manifest-v1\n" + encoded + b"\n"

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
