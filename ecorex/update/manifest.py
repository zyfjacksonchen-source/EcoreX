"""Versioned, signed release manifest contracts for the EcoreX v1 updater.

The manifest module intentionally performs no I/O.  It is the trust-boundary
parser shared by the release tooling and the local install coordinator.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CORE_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_CAPABILITY_PACK_ARTIFACT_BYTES = 500 * 1024 * 1024
# Transport/storage code uses the largest admissible artifact as its absolute
# allocation ceiling. ReleaseArtifact applies the narrower identity-specific
# limit below, so this does not relax the 150 MiB Core contract.
MAX_ARTIFACT_BYTES = MAX_CAPABILITY_PACK_ARTIFACT_BYTES
MAX_PORTABLE_SEGMENT_BYTES = 120
_HEX_256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')


class ManifestError(ValueError):
    """Raised when untrusted release metadata violates the v1 contract."""


class SourceKind(StrEnum):
    """Supported release origins, in required failover order."""

    GITHUB_CN_MIRROR = "github-cn-mirror"
    GITHUB_RELEASE = "github-release"
    ECOREX_CDN = "ecorex-cdn"


SOURCE_PRIORITY: tuple[SourceKind, ...] = (
    SourceKind.GITHUB_CN_MIRROR,
    SourceKind.GITHUB_RELEASE,
    SourceKind.ECOREX_CDN,
)


class ReleaseChannel(StrEnum):
    CANARY = "canary"
    STABLE = "stable"


@dataclass(frozen=True, slots=True)
class SignatureEnvelope:
    """A detached signature plus the key identity required to verify it."""

    algorithm: str
    key_id: str
    value: str

    def __post_init__(self) -> None:
        if self.algorithm != "ed25519":
            raise ManifestError("v1 release signatures must use ed25519")
        if not _SAFE_ID_RE.fullmatch(self.key_id):
            raise ManifestError("signature key_id is missing or unsafe")
        if not self.value or len(self.value) > 2048:
            raise ManifestError("signature value is missing or unreasonably large")
        try:
            base64.b64decode(self.value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ManifestError("signature value must be canonical base64") from exc

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SignatureEnvelope":
        _require_exact_keys(raw, {"algorithm", "key_id", "value"}, "signature")
        return cls(
            algorithm=_require_str(raw, "algorithm"),
            key_id=_require_str(raw, "key_id"),
            value=_require_str(raw, "value"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ReleaseSource:
    """One HTTPS source for all artifacts in a release."""

    source_id: str
    kind: SourceKind
    priority: int
    base_url: str

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.source_id):
            raise ManifestError("release source_id is missing or unsafe")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise ManifestError("release source priority must be a non-negative integer")
        normalized_url = self.base_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ManifestError("release source base_url must be an absolute HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ManifestError("release source base_url cannot contain credentials, query, or fragment")
        try:
            encoded_url = normalized_url.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ManifestError("release source base_url is not valid Unicode") from exc
        if len(encoded_url) > 2048:
            raise ManifestError("release source base_url is too long")
        object.__setattr__(self, "base_url", normalized_url)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReleaseSource":
        _require_exact_keys(raw, {"source_id", "kind", "priority", "base_url"}, "source")
        priority = raw.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ManifestError("source.priority must be an integer")
        try:
            kind = SourceKind(_require_str(raw, "kind"))
        except ValueError as exc:
            raise ManifestError(f"unsupported release source kind: {raw.get('kind')!r}") from exc
        return cls(
            source_id=_require_str(raw, "source_id"),
            kind=kind,
            priority=priority,
            base_url=_require_str(raw, "base_url").rstrip("/"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "priority": self.priority,
            "base_url": self.base_url,
        }

    def artifact_url(self, artifact: "ReleaseArtifact") -> str:
        # artifact.file_name is guaranteed to be a single safe path segment.
        return f"{self.base_url}/{artifact.file_name}"


def _artifact_size_limit(artifact_id: str) -> int:
    if artifact_id.startswith("capability-pack-"):
        if artifact_id.endswith("-manifest"):
            return MAX_MANIFEST_BYTES
        return MAX_CAPABILITY_PACK_ARTIFACT_BYTES
    return MAX_CORE_ARTIFACT_BYTES


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    artifact_id: str
    platform: str
    architecture: str
    file_name: str
    size_bytes: int
    sha256: str
    signature: SignatureEnvelope

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.artifact_id):
            raise ManifestError("artifact_id is missing or unsafe")
        if not _SAFE_ID_RE.fullmatch(self.platform):
            raise ManifestError("artifact platform is missing or unsafe")
        if not _SAFE_ID_RE.fullmatch(self.architecture):
            raise ManifestError("artifact architecture is missing or unsafe")
        path = PurePosixPath(self.file_name.replace("\\", "/"))
        if (
            not self.file_name
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or ":" in self.file_name
        ):
            raise ManifestError("artifact file_name must be one safe path segment")
        validate_portable_path_segment(self.file_name, label="artifact file_name")
        size_limit = _artifact_size_limit(self.artifact_id)
        if (
            isinstance(self.size_bytes, bool)
            or self.size_bytes <= 0
            or self.size_bytes > size_limit
        ):
            raise ManifestError(
                f"artifact size_bytes must be between 1 and {size_limit}"
            )
        if not _HEX_256_RE.fullmatch(self.sha256):
            raise ManifestError("artifact sha256 must contain exactly 64 hexadecimal characters")
        object.__setattr__(self, "sha256", self.sha256.lower())

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReleaseArtifact":
        _require_exact_keys(
            raw,
            {
                "artifact_id",
                "platform",
                "architecture",
                "file_name",
                "size_bytes",
                "sha256",
                "signature",
            },
            "artifact",
        )
        size = raw.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ManifestError("artifact.size_bytes must be an integer")
        signature = raw.get("signature")
        if not isinstance(signature, Mapping):
            raise ManifestError("artifact.signature must be an object")
        return cls(
            artifact_id=_require_str(raw, "artifact_id"),
            platform=_require_str(raw, "platform"),
            architecture=_require_str(raw, "architecture"),
            file_name=_require_str(raw, "file_name"),
            size_bytes=size,
            sha256=_require_str(raw, "sha256"),
            signature=SignatureEnvelope.from_dict(signature),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "platform": self.platform,
            "architecture": self.architecture,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "signature": self.signature.to_dict(),
        }

    def signed_payload(self, *, release_id: str, version: str, build_digest: str) -> bytes:
        """Return the domain-separated bytes covered by the artifact signature."""

        fields = (
            "ecorex-artifact-v1",
            release_id,
            version,
            build_digest,
            self.artifact_id,
            self.platform,
            self.architecture,
            self.file_name,
            str(self.size_bytes),
            self.sha256,
        )
        return ("\n".join(fields) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """The immutable release identity and all download trust metadata."""

    schema_version: int
    release_id: str
    version: str
    build_digest: str
    channel: ReleaseChannel
    created_at: str
    sources: tuple[ReleaseSource, ...]
    artifacts: tuple[ReleaseArtifact, ...]
    signature: SignatureEnvelope

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                f"unsupported manifest schema_version {self.schema_version!r}; "
                f"expected {MANIFEST_SCHEMA_VERSION}"
            )
        if not _SAFE_ID_RE.fullmatch(self.release_id):
            raise ManifestError("release_id is missing or unsafe")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ManifestError("release version must be valid SemVer")
        core_and_prerelease = self.version.split("+", 1)[0]
        _core, separator, prerelease = core_and_prerelease.partition("-")
        if separator and any(
            identifier.isdigit()
            and len(identifier) > 1
            and identifier.startswith("0")
            for identifier in prerelease.split(".")
        ):
            raise ManifestError("numeric SemVer prerelease identifiers cannot have leading zeroes")
        if len(self.version.encode("utf-8")) > 128:
            raise ManifestError("release version is too long")
        if not _HEX_256_RE.fullmatch(self.build_digest):
            raise ManifestError("build_digest must contain exactly 64 hexadecimal characters")
        object.__setattr__(self, "build_digest", self.build_digest.lower())
        if not isinstance(self.channel, ReleaseChannel):
            raise ManifestError("release channel is invalid")
        _require_utc_or_offset_timestamp(self.created_at)
        kinds = tuple(source.kind for source in self.sources)
        priorities = tuple(source.priority for source in self.sources)
        if kinds != SOURCE_PRIORITY or priorities != tuple(range(len(SOURCE_PRIORITY))):
            expected = ", ".join(kind.value for kind in SOURCE_PRIORITY)
            raise ManifestError(f"release sources must be ordered exactly as: {expected}")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ManifestError("release source_id values must be unique")
        source_hosts = [
            (urlparse(source.base_url).hostname or "").casefold().rstrip(".")
            for source in self.sources
        ]
        if len(source_hosts) != len(set(source_hosts)):
            raise ManifestError(
                "release sources must use distinct download hosts for real failover"
            )
        if not self.artifacts or len(self.artifacts) > 64:
            raise ManifestError("release manifest must contain between 1 and 64 artifacts")
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ManifestError("release artifact_id values must be unique")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReleaseManifest":
        _require_exact_keys(
            raw,
            {
                "schema_version",
                "release_id",
                "version",
                "build_digest",
                "channel",
                "created_at",
                "sources",
                "artifacts",
                "signature",
            },
            "manifest",
        )
        schema_version = raw.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ManifestError("manifest.schema_version must be an integer")
        try:
            channel = ReleaseChannel(_require_str(raw, "channel"))
        except ValueError as exc:
            raise ManifestError(f"unsupported release channel: {raw.get('channel')!r}") from exc
        sources_raw = raw.get("sources")
        artifacts_raw = raw.get("artifacts")
        signature_raw = raw.get("signature")
        if not _is_object_sequence(sources_raw):
            raise ManifestError("manifest.sources must be an array of objects")
        if not _is_object_sequence(artifacts_raw):
            raise ManifestError("manifest.artifacts must be an array of objects")
        if not isinstance(signature_raw, Mapping):
            raise ManifestError("manifest.signature must be an object")
        return cls(
            schema_version=schema_version,
            release_id=_require_str(raw, "release_id"),
            version=_require_str(raw, "version"),
            build_digest=_require_str(raw, "build_digest"),
            channel=channel,
            created_at=_require_str(raw, "created_at"),
            sources=tuple(ReleaseSource.from_dict(item) for item in sources_raw),
            artifacts=tuple(ReleaseArtifact.from_dict(item) for item in artifacts_raw),
            signature=SignatureEnvelope.from_dict(signature_raw),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "ReleaseManifest":
        try:
            payload_size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        except UnicodeEncodeError as exc:
            raise ManifestError("release manifest is not valid UTF-8 JSON") from exc
        if payload_size > MAX_MANIFEST_BYTES:
            raise ManifestError("release manifest exceeds the 1 MiB hard limit")
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError("release manifest is not valid UTF-8 JSON") from exc
        if not isinstance(raw, Mapping):
            raise ManifestError("release manifest root must be an object")
        return cls.from_dict(raw)

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "version": self.version,
            "build_digest": self.build_digest,
            "channel": self.channel.value,
            "created_at": self.created_at,
            "sources": [source.to_dict() for source in self.sources],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }
        if include_signature:
            result["signature"] = self.signature.to_dict()
        return result

    def canonical_payload(self) -> bytes:
        """Return deterministic, domain-separated bytes covered by the manifest signature."""

        canonical_json = json.dumps(
            self.to_dict(include_signature=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return b"ecorex-release-manifest-v1\n" + canonical_json + b"\n"

    def to_json(self, *, include_signature: bool = True, pretty: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_signature=include_signature),
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )

    def artifact(self, artifact_id: str) -> ReleaseArtifact:
        for artifact in self.artifacts:
            if artifact.artifact_id == artifact_id:
                return artifact
        raise ManifestError(f"artifact {artifact_id!r} is not present in release {self.release_id!r}")


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(raw)
    missing = expected - actual
    unexpected = actual - expected
    if missing or unexpected:
        fragments: list[str] = []
        if missing:
            fragments.append(f"missing {sorted(missing)!r}")
        if unexpected:
            fragments.append(f"unexpected {sorted(unexpected)!r}")
        raise ManifestError(f"{label} has invalid fields: {', '.join(fragments)}")


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{key} must be a non-empty string")
    return value


def _is_object_sequence(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and all(isinstance(item, Mapping) for item in value)
    )


def _require_utc_or_offset_timestamp(value: str) -> None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ManifestError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError("created_at must include a UTC offset")


def portable_path_segment_key(value: str) -> str:
    """Return the cross-platform collision key for one path segment."""

    return unicodedata.normalize("NFKC", value).casefold()


def validate_portable_path_segment(value: str, *, label: str = "path segment") -> None:
    """Reject names that are ambiguous or unsafe on Windows and macOS."""

    normalized = unicodedata.normalize("NFKC", value)
    try:
        utf8_size = len(normalized.encode("utf-8"))
        utf16_units = len(normalized.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ManifestError(f"{label} is not valid Unicode") from exc
    if (
        not normalized
        or normalized != value
        or normalized in {".", ".."}
        or normalized != normalized.rstrip(" .")
        or any(ord(character) < 32 for character in normalized)
        or any(character in _WINDOWS_FORBIDDEN for character in normalized)
        or utf8_size > MAX_PORTABLE_SEGMENT_BYTES
        or utf16_units > MAX_PORTABLE_SEGMENT_BYTES
    ):
        raise ManifestError(f"{label} is not a portable path segment")
    device_stem = normalized.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_DEVICE_NAMES:
        raise ManifestError(f"{label} uses a reserved Windows device name")
