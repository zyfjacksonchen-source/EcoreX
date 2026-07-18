"""Release-key-signed, monotonic discovery for EcoreX Bootstrap artifacts.

The outer authority authenticates freshness and the exact target identity. It
does not replace manifest/artifact verification: Bootstrap must still verify
the exact manifest and artifact signatures with its compiled Ed25519 trust
store before installing any bytes.
"""

from __future__ import annotations

import base64
import copy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
import tempfile
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from ecorex.update import (
    MAX_MANIFEST_BYTES,
    ReleaseChannel,
    ReleaseManifest,
    SignatureEnvelope,
    SignatureVerifier,
    verify_artifact_signature,
    verify_manifest_signature,
)
from ecorex.update.locking import ProductFileLock

from .builder import MAX_BOOTSTRAP_BYTES
from .publication_policy import (
    publication_receipt_policy,
    required_publication_sources,
)
from .signing import ReleaseSigner, sign_envelope


PUBLIC_BOOTSTRAP_INDEX_SCHEMA_VERSION = 1
PUBLIC_BOOTSTRAP_INDEX_DOCUMENT_TYPE = "ecorex.public-bootstrap-discovery"
PUBLIC_BOOTSTRAP_INDEX_TRUST = "untrusted-discovery-hint"
PUBLIC_BOOTSTRAP_AUTHORITY_DOMAIN = "ecorex.public-bootstrap-pointer-authority.v1"
PUBLIC_BOOTSTRAP_FRESHNESS_DOMAIN = "ecorex.public-bootstrap-freshness.v1"
PUBLIC_BOOTSTRAP_INDEX_FILE_NAME = "public-bootstrap-index.json"
MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES = 256 * 1024
PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS = 24 * 60 * 60
PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS = 5 * 60

_SHA256_LENGTH = 64
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_STABLE_RELEASE_ID = re.compile(r"^release-stable-[0-9a-f]{24}$")
_V1_VERSION = re.compile(
    r"^1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:[-+][0-9A-Za-z.-]+)?$"
)
_STABLE_SEQUENCE_VERSION = re.compile(r"^1\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_AUTHORITY_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_LEGACY_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "release_id",
        "version",
        "manifest_sha256",
        "github_release_id",
        "github_draft",
        "source_receipts",
    }
)
_PRIMARY_ONLY_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "release_id",
        "version",
        "manifest_sha256",
        "publication_policy",
        "source_receipts",
    }
)
_RECEIPT_ASSET_KEYS = frozenset({"name", "size_bytes", "sha256", "url"})
_RESERVED_RELEASE_FILES = frozenset(
    {"release-manifest.json", "release-metadata.json", "sbom.cdx.json"}
)
_BOOTSTRAP_TARGETS = (
    ("bootstrap-windows-x64", "windows", "x64"),
    ("bootstrap-macos-arm64", "macos", "arm64"),
    ("bootstrap-macos-x64", "macos", "x64"),
)


class PublicBootstrapIndexError(ValueError):
    """A stable release cannot be projected into a public discovery index."""


def unpublished_public_bootstrap_index() -> dict[str, object]:
    """Return the only safe checked-in state before a real release exists."""

    return {
        "schema_version": PUBLIC_BOOTSTRAP_INDEX_SCHEMA_VERSION,
        "document_type": PUBLIC_BOOTSTRAP_INDEX_DOCUMENT_TYPE,
        "trust": PUBLIC_BOOTSTRAP_INDEX_TRUST,
        "status": "unpublished",
        "authority": None,
        "freshness": None,
        "release": None,
    }


def build_public_bootstrap_index(
    *,
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    manifest_sha256: str,
    publication_receipt: Mapping[str, Any],
    publication_receipt_sha256: str,
    verifier: SignatureVerifier,
    freshness_verifier: SignatureVerifier,
    signer: ReleaseSigner | None = None,
    authority_signature: SignatureEnvelope | None = None,
    freshness_signer: ReleaseSigner | None = None,
    freshness_signature: SignatureEnvelope | None = None,
    freshness_issued_at: str | None = None,
    freshness_expires_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Project one fully published stable release into a signed pointer.

    The caller still has to verify local release files before publication.  This
    boundary independently verifies the signed manifest and all three Bootstrap
    artifact signatures, then proves the channel-required published bytes. A
    Stable pointer advertises only its verified domestic primary source; the
    signed manifest retains the full client failover order for later updates.
    """

    if not isinstance(manifest, ReleaseManifest):
        raise TypeError("manifest must be a ReleaseManifest")
    if manifest.channel is not ReleaseChannel.STABLE:
        raise PublicBootstrapIndexError(
            "the public Bootstrap index can only expose the stable channel"
        )
    _require_sha256(manifest_sha256, "manifest_sha256")
    _require_sha256(publication_receipt_sha256, "publication_receipt_sha256")
    if not isinstance(publication_receipt, Mapping):
        raise PublicBootstrapIndexError("publication receipt must be an object")
    if (signer is None) == (authority_signature is None):
        raise PublicBootstrapIndexError(
            "exactly one pointer signer or existing authority signature is required"
        )
    if (freshness_signer is None) == (freshness_signature is None):
        raise PublicBootstrapIndexError(
            "exactly one freshness signer or existing freshness signature is required"
        )

    _verify_exact_manifest_bytes(manifest, manifest_bytes, manifest_sha256)
    verify_manifest_signature(manifest, verifier)
    bootstrap_artifacts = []
    expected_bootstrap_ids = {target[0] for target in _BOOTSTRAP_TARGETS}
    observed_bootstrap_ids = {
        artifact.artifact_id
        for artifact in manifest.artifacts
        if artifact.artifact_id.startswith("bootstrap-")
    }
    if observed_bootstrap_ids != expected_bootstrap_ids:
        raise PublicBootstrapIndexError(
            "stable publication requires exactly the Windows x64, macOS arm64 "
            "and macOS x64 Bootstrap artifacts"
        )
    for artifact_id, platform, architecture in _BOOTSTRAP_TARGETS:
        artifact = manifest.artifact(artifact_id)
        if (artifact.platform, artifact.architecture) != (platform, architecture):
            raise PublicBootstrapIndexError(
                f"Bootstrap target differs for {artifact_id!r}"
            )
        if artifact.size_bytes > MAX_BOOTSTRAP_BYTES:
            raise PublicBootstrapIndexError(
                f"Bootstrap {artifact_id!r} exceeds the 10 MiB public limit"
            )
        verify_artifact_signature(manifest, artifact, verifier)
        bootstrap_artifacts.append(artifact)

    receipt_assets = _validate_publication_receipt(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        receipt=publication_receipt,
        receipt_sha256=publication_receipt_sha256,
    )

    def source_links(file_name: str) -> list[dict[str, object]]:
        return [
            {
                "source_id": source.source_id,
                "kind": source.kind.value,
                "priority": source.priority,
                "url": receipt_assets[source.source_id][file_name][2],
            }
            for source in required_publication_sources(manifest)
        ]

    observed_now = _utc_now(now)
    if freshness_issued_at is None and freshness_expires_at is None:
        freshness_issued_at = _format_authority_time(observed_now)
        freshness_expires_at = _format_authority_time(
            observed_now + timedelta(seconds=PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS)
        )
    elif freshness_issued_at is None or freshness_expires_at is None:
        raise PublicBootstrapIndexError(
            "public pointer authority freshness window is incomplete"
        )
    _validate_authority_window(
        freshness_issued_at,
        freshness_expires_at,
        now=observed_now,
    )

    target = {
        "manifest_sha256": manifest_sha256,
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
    }
    sequence = stable_pointer_sequence(manifest.version)
    revision = manifest.release_id
    signing_payload = public_bootstrap_authority_signing_bytes(
        sequence=sequence,
        revision=revision,
        target=target,
    )
    signature = (
        sign_envelope(signer, signing_payload)
        if signer is not None
        else authority_signature
    )
    assert signature is not None
    try:
        verdict = verifier.verify(signing_payload, signature)
    except Exception as exc:
        raise PublicBootstrapIndexError(
            f"public pointer authority signature is invalid: {type(exc).__name__}"
        ) from None
    if verdict is not True:
        raise PublicBootstrapIndexError(
            "public pointer authority signature was rejected"
        )
    authority_sha256 = hashlib.sha256(signing_payload).hexdigest()
    freshness_payload = public_bootstrap_freshness_signing_bytes(
        authority_sha256=authority_sha256,
        issued_at=freshness_issued_at,
        expires_at=freshness_expires_at,
    )
    freshness = (
        sign_envelope(freshness_signer, freshness_payload)
        if freshness_signer is not None
        else freshness_signature
    )
    assert freshness is not None
    if _trust_roles_overlap(
        authority_key_id=signature.key_id,
        freshness_key_id=freshness.key_id,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        authority_signer=signer,
        freshness_signer=freshness_signer,
    ):
        raise PublicBootstrapIndexError(
            "freshness and immutable release authority must use distinct keys"
        )
    try:
        freshness_verdict = freshness_verifier.verify(freshness_payload, freshness)
    except Exception as exc:
        raise PublicBootstrapIndexError(
            f"public pointer freshness signature is invalid: {type(exc).__name__}"
        ) from None
    if freshness_verdict is not True:
        raise PublicBootstrapIndexError(
            "public pointer freshness signature was rejected"
        )

    index = {
        "schema_version": PUBLIC_BOOTSTRAP_INDEX_SCHEMA_VERSION,
        "document_type": PUBLIC_BOOTSTRAP_INDEX_DOCUMENT_TYPE,
        "trust": PUBLIC_BOOTSTRAP_INDEX_TRUST,
        "status": "published",
        "authority": {
            "sequence": sequence,
            "revision": revision,
            "target": target,
            "signature": signature.to_dict(),
        },
        "freshness": {
            "authority_sha256": authority_sha256,
            "issued_at": freshness_issued_at,
            "expires_at": freshness_expires_at,
            "signature": freshness.to_dict(),
        },
        "release": {
            "release_id": manifest.release_id,
            "version": manifest.version,
            "channel": manifest.channel.value,
            "created_at": manifest.created_at,
            "build_digest": manifest.build_digest,
            "publication_receipt_sha256": publication_receipt_sha256,
            "manifest": {
                "file_name": "release-manifest.json",
                "sha256": manifest_sha256,
                "signature": manifest.signature.to_dict(),
                "sources": source_links("release-manifest.json"),
            },
            "bootstrap_artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "platform": artifact.platform,
                    "architecture": artifact.architecture,
                    "file_name": artifact.file_name,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "signature": artifact.signature.to_dict(),
                    "sources": source_links(artifact.file_name),
                }
                for artifact in bootstrap_artifacts
            ],
        },
    }
    validate_public_bootstrap_index(
        index,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        now=observed_now,
    )
    return index


def stable_pointer_sequence(version: str) -> int:
    """Map one immutable v1 stable SemVer to its monotonic pointer sequence."""

    match = (
        _STABLE_SEQUENCE_VERSION.fullmatch(version)
        if isinstance(version, str)
        else None
    )
    if match is None:
        raise PublicBootstrapIndexError(
            "stable public pointer version must be a final v1 SemVer"
        )
    minor = int(match.group(1))
    patch = int(match.group(2))
    # v1.0.0 is sequence 1. Each minor owns one million patch positions.  This
    # remains exactly representable by JavaScript and int64 for the schema's
    # bounded six-digit SemVer components.
    return minor * 1_000_000 + patch + 1


def public_bootstrap_authority_signing_bytes(
    *,
    sequence: int,
    revision: str,
    target: Mapping[str, Any],
) -> bytes:
    """Cross-language, domain-separated bytes signed by the release key."""

    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= 999_999_999_999
        or not _matches(revision, _STABLE_RELEASE_ID)
        or not isinstance(target, Mapping)
        or set(target) != {"manifest_sha256", "release_id", "version", "build_digest"}
        or not _is_sha256_value(target.get("manifest_sha256"))
        or target.get("release_id") != revision
        or not _matches(target.get("version"), _STABLE_SEQUENCE_VERSION)
        or not _is_sha256_value(target.get("build_digest"))
    ):
        raise PublicBootstrapIndexError("public authority signing identity is invalid")
    parts = (
        PUBLIC_BOOTSTRAP_AUTHORITY_DOMAIN,
        str(sequence),
        revision,
        str(target["manifest_sha256"]),
        str(target["release_id"]),
        str(target["version"]),
        str(target["build_digest"]),
    )
    return "\0".join(parts).encode("ascii")


def public_bootstrap_freshness_signing_bytes(
    *,
    authority_sha256: str,
    issued_at: str,
    expires_at: str,
) -> bytes:
    if (
        not _is_sha256_value(authority_sha256)
        or not _is_authority_time(issued_at)
        or not _is_authority_time(expires_at)
    ):
        raise PublicBootstrapIndexError("public freshness signing identity is invalid")
    return "\0".join(
        (
            PUBLIC_BOOTSTRAP_FRESHNESS_DOMAIN,
            authority_sha256,
            issued_at,
            expires_at,
        )
    ).encode("ascii")


def validate_public_bootstrap_index(
    index: Mapping[str, Any],
    *,
    verifier: SignatureVerifier | None = None,
    freshness_verifier: SignatureVerifier | None = None,
    now: datetime | None = None,
    allow_expired_freshness: bool = False,
) -> None:
    """Fail closed unless *index* is one exact v1 discovery document.

    This runtime validator is deliberately independent of the checked-in JSON
    Schema and browser parser.  The release command therefore cannot replace a
    live pointer with a structurally looser document even if a caller bypasses
    :func:`build_public_bootstrap_index`.
    """

    root = _require_mapping(index, "public index")
    _require_exact_keys(
        root,
        {
            "schema_version",
            "document_type",
            "trust",
            "status",
            "authority",
            "freshness",
            "release",
        },
        "public index",
    )
    if (
        root.get("schema_version") != PUBLIC_BOOTSTRAP_INDEX_SCHEMA_VERSION
        or root.get("document_type") != PUBLIC_BOOTSTRAP_INDEX_DOCUMENT_TYPE
        or root.get("trust") != PUBLIC_BOOTSTRAP_INDEX_TRUST
    ):
        raise PublicBootstrapIndexError("public index identity is invalid")
    if root.get("status") == "unpublished":
        if (
            root.get("release") is not None
            or root.get("authority") is not None
            or root.get("freshness") is not None
        ):
            raise PublicBootstrapIndexError(
                "an unpublished public index cannot contain authority or release data"
            )
        return
    if root.get("status") != "published":
        raise PublicBootstrapIndexError("public index status is invalid")

    release = _require_mapping(root.get("release"), "public release")
    _require_exact_keys(
        release,
        {
            "release_id",
            "version",
            "channel",
            "created_at",
            "build_digest",
            "publication_receipt_sha256",
            "manifest",
            "bootstrap_artifacts",
        },
        "public release",
    )
    if (
        not _matches(release.get("release_id"), _STABLE_RELEASE_ID)
        or not _matches(release.get("version"), _V1_VERSION)
        or release.get("channel") != "stable"
        or not _is_aware_datetime(release.get("created_at"))
        or not _is_sha256_value(release.get("build_digest"))
        or not _is_sha256_value(release.get("publication_receipt_sha256"))
    ):
        raise PublicBootstrapIndexError("public release identity is invalid")

    authority = _require_mapping(root.get("authority"), "public authority")
    _require_exact_keys(
        authority,
        {"sequence", "revision", "target", "signature"},
        "public authority",
    )
    target = _require_mapping(authority.get("target"), "public authority target")
    _require_exact_keys(
        target,
        {"manifest_sha256", "release_id", "version", "build_digest"},
        "public authority target",
    )
    expected_sequence = stable_pointer_sequence(str(release.get("version") or ""))
    if (
        isinstance(authority.get("sequence"), bool)
        or authority.get("sequence") != expected_sequence
        or authority.get("revision") != release.get("release_id")
        or target.get("manifest_sha256")
        != _require_mapping(release.get("manifest"), "public manifest").get("sha256")
        or target.get("release_id") != release.get("release_id")
        or target.get("version") != release.get("version")
        or target.get("build_digest") != release.get("build_digest")
    ):
        raise PublicBootstrapIndexError("public authority target is inconsistent")
    _validate_signature(authority.get("signature"), "public authority signature")
    if verifier is not None:
        try:
            signature = SignatureEnvelope.from_dict(
                _require_mapping(
                    authority.get("signature"), "public authority signature"
                )
            )
            verdict = verifier.verify(
                public_bootstrap_authority_signing_bytes(
                    sequence=expected_sequence,
                    revision=str(authority["revision"]),
                    target=target,
                ),
                signature,
            )
        except Exception as exc:
            raise PublicBootstrapIndexError(
                f"public authority signature is invalid: {type(exc).__name__}"
            ) from None
        if verdict is not True:
            raise PublicBootstrapIndexError("public authority signature was rejected")

    freshness = _require_mapping(root.get("freshness"), "public freshness")
    _require_exact_keys(
        freshness,
        {"authority_sha256", "issued_at", "expires_at", "signature"},
        "public freshness",
    )
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=expected_sequence,
        revision=str(authority["revision"]),
        target=target,
    )
    authority_sha256 = hashlib.sha256(authority_payload).hexdigest()
    if freshness.get("authority_sha256") != authority_sha256:
        raise PublicBootstrapIndexError(
            "public freshness does not bind the immutable authority"
        )
    issued_at = freshness.get("issued_at")
    expires_at = freshness.get("expires_at")
    _validate_authority_window(
        issued_at,
        expires_at,
        now=_utc_now(now),
        allow_expired=allow_expired_freshness,
    )
    _validate_signature(freshness.get("signature"), "public freshness signature")
    authority_key_id = _require_mapping(
        authority.get("signature"), "public authority signature"
    ).get("key_id")
    freshness_key_id = _require_mapping(
        freshness.get("signature"), "public freshness signature"
    ).get("key_id")
    if _trust_roles_overlap(
        authority_key_id=str(authority_key_id),
        freshness_key_id=str(freshness_key_id),
        verifier=verifier,
        freshness_verifier=freshness_verifier,
    ):
        raise PublicBootstrapIndexError(
            "freshness and immutable release authority must use distinct keys"
        )
    if freshness_verifier is not None:
        try:
            freshness_signature_value = SignatureEnvelope.from_dict(
                _require_mapping(
                    freshness.get("signature"), "public freshness signature"
                )
            )
            freshness_verdict = freshness_verifier.verify(
                public_bootstrap_freshness_signing_bytes(
                    authority_sha256=authority_sha256,
                    issued_at=str(issued_at),
                    expires_at=str(expires_at),
                ),
                freshness_signature_value,
            )
        except Exception as exc:
            raise PublicBootstrapIndexError(
                f"public freshness signature is invalid: {type(exc).__name__}"
            ) from None
        if freshness_verdict is not True:
            raise PublicBootstrapIndexError("public freshness signature was rejected")

    manifest = _require_mapping(release.get("manifest"), "public manifest")
    _require_exact_keys(
        manifest,
        {"file_name", "sha256", "signature", "sources"},
        "public manifest",
    )
    if manifest.get("file_name") != "release-manifest.json" or not _is_sha256_value(
        manifest.get("sha256")
    ):
        raise PublicBootstrapIndexError("public manifest identity is invalid")
    _validate_signature(manifest.get("signature"), "public manifest signature")
    manifest_sources, source_bases = _validate_sources(
        manifest.get("sources"),
        file_name="release-manifest.json",
        label="public manifest sources",
    )

    artifacts = release.get("bootstrap_artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(_BOOTSTRAP_TARGETS):
        raise PublicBootstrapIndexError(
            "public index requires exactly three Bootstrap artifacts"
        )
    for position, (artifact_id, platform, architecture) in enumerate(
        _BOOTSTRAP_TARGETS
    ):
        artifact = _require_mapping(
            artifacts[position], f"public Bootstrap {artifact_id}"
        )
        _require_exact_keys(
            artifact,
            {
                "artifact_id",
                "platform",
                "architecture",
                "file_name",
                "size_bytes",
                "sha256",
                "signature",
                "sources",
            },
            f"public Bootstrap {artifact_id}",
        )
        file_name = artifact.get("file_name")
        size_bytes = artifact.get("size_bytes")
        if (
            artifact.get("artifact_id") != artifact_id
            or artifact.get("platform") != platform
            or artifact.get("architecture") != architecture
            or not _matches(file_name, _SAFE_FILE_NAME)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= MAX_BOOTSTRAP_BYTES
            or not _is_sha256_value(artifact.get("sha256"))
        ):
            raise PublicBootstrapIndexError(
                f"public Bootstrap {artifact_id!r} identity is invalid"
            )
        _validate_signature(
            artifact.get("signature"), f"public Bootstrap {artifact_id} signature"
        )
        artifact_sources, artifact_bases = _validate_sources(
            artifact.get("sources"),
            file_name=file_name,
            label=f"public Bootstrap {artifact_id} sources",
        )
        if artifact_sources != manifest_sources or artifact_bases != source_bases:
            raise PublicBootstrapIndexError(
                f"public Bootstrap {artifact_id!r} source identity differs"
            )


def refresh_public_bootstrap_freshness(
    index: Mapping[str, Any],
    *,
    verifier: SignatureVerifier,
    freshness_verifier: SignatureVerifier,
    freshness_signer: ReleaseSigner,
    issued_at: str,
    expires_at: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Re-sign only freshness while preserving immutable authority and target."""

    observed_now = _utc_now(now)
    validate_public_bootstrap_index(
        index,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        now=observed_now,
        allow_expired_freshness=True,
    )
    if index.get("status") != "published":
        raise PublicBootstrapIndexError(
            "only a published public pointer can refresh freshness"
        )
    _validate_authority_window(
        issued_at,
        expires_at,
        now=observed_now,
    )
    authority = _require_mapping(index.get("authority"), "public authority")
    target = _require_mapping(authority.get("target"), "public authority target")
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=int(authority["sequence"]),
        revision=str(authority["revision"]),
        target=target,
    )
    authority_sha256 = hashlib.sha256(authority_payload).hexdigest()
    freshness_payload = public_bootstrap_freshness_signing_bytes(
        authority_sha256=authority_sha256,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    signature = sign_envelope(freshness_signer, freshness_payload)
    authority_key_id = _require_mapping(
        authority.get("signature"), "public authority signature"
    ).get("key_id")
    if _trust_roles_overlap(
        authority_key_id=str(authority_key_id),
        freshness_key_id=signature.key_id,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        freshness_signer=freshness_signer,
    ):
        raise PublicBootstrapIndexError(
            "freshness and immutable release authority must use distinct keys"
        )
    try:
        verdict = freshness_verifier.verify(freshness_payload, signature)
    except Exception as exc:
        raise PublicBootstrapIndexError(
            f"public pointer freshness signature is invalid: {type(exc).__name__}"
        ) from None
    if verdict is not True:
        raise PublicBootstrapIndexError(
            "public pointer freshness signature was rejected"
        )
    refreshed = copy.deepcopy(dict(index))
    refreshed["freshness"] = {
        "authority_sha256": authority_sha256,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "signature": signature.to_dict(),
    }
    validate_public_bootstrap_index(
        refreshed,
        verifier=verifier,
        freshness_verifier=freshness_verifier,
        now=observed_now,
    )
    return refreshed


def write_public_bootstrap_index(
    path_value: str | os.PathLike[str],
    index: Mapping[str, Any],
) -> tuple[Path, str]:
    """Atomically replace a public pointer while holding a product file lock."""

    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise PublicBootstrapIndexError("public index parent is not a directory")
    path = parent / path.name
    if path.name != PUBLIC_BOOTSTRAP_INDEX_FILE_NAME:
        raise PublicBootstrapIndexError(
            f"public index output must be named {PUBLIC_BOOTSTRAP_INDEX_FILE_NAME!r}"
        )
    _require_safe_existing_file(path)
    validate_public_bootstrap_index(index)
    payload = _canonical_json_bytes(index) + b"\n"
    if len(payload) > MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES:
        raise PublicBootstrapIndexError("public index exceeds the 256 KiB limit")
    digest = hashlib.sha256(payload).hexdigest()
    lock_identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
    lock_path = (
        Path(tempfile.gettempdir()) / f"ecorex-public-index-{lock_identity}.lock"
    )
    with ProductFileLock(lock_path, timeout=0):
        _require_safe_existing_file(path)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{digest[:16]}")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return path, digest


def _verify_exact_manifest_bytes(
    manifest: ReleaseManifest,
    manifest_bytes: bytes,
    manifest_sha256: str,
) -> None:
    if (
        not isinstance(manifest_bytes, bytes)
        or not 1 <= len(manifest_bytes) <= MAX_MANIFEST_BYTES
    ):
        raise PublicBootstrapIndexError("exact signed manifest bytes are invalid")
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise PublicBootstrapIndexError(
            "manifest_sha256 differs from the exact signed manifest bytes"
        )
    try:
        parsed = ReleaseManifest.from_json(manifest_bytes)
    except (TypeError, ValueError, RecursionError):
        raise PublicBootstrapIndexError(
            "exact signed manifest bytes are not a valid v1 manifest"
        ) from None
    if parsed != manifest:
        raise PublicBootstrapIndexError(
            "exact signed manifest bytes describe a different manifest"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicBootstrapIndexError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise PublicBootstrapIndexError(f"{label} shape is invalid")


def _matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_sha256_value(value: Any) -> bool:
    return isinstance(value, str) and _is_sha256(value)


def _is_aware_datetime(value: Any) -> bool:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _utc_now(value: datetime | None) -> datetime:
    observed = datetime.now(UTC) if value is None else value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise PublicBootstrapIndexError("public pointer clock must be timezone-aware")
    return observed.astimezone(UTC).replace(microsecond=0)


def _format_authority_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_authority_time(value: Any) -> bool:
    if not isinstance(value, str) or _AUTHORITY_TIME.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return _format_authority_time(parsed) == value


def _validate_authority_window(
    issued_at: Any,
    expires_at: Any,
    *,
    now: datetime,
    allow_expired: bool = False,
) -> None:
    if not _is_authority_time(issued_at) or not _is_authority_time(expires_at):
        raise PublicBootstrapIndexError(
            "public pointer authority freshness is not canonical UTC"
        )
    issued = datetime.strptime(issued_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    expires = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    lifetime = int((expires - issued).total_seconds())
    if not 1 <= lifetime <= PUBLIC_BOOTSTRAP_AUTHORITY_MAX_TTL_SECONDS:
        raise PublicBootstrapIndexError(
            "public pointer authority freshness lifetime is invalid"
        )
    if issued > now + timedelta(seconds=PUBLIC_BOOTSTRAP_AUTHORITY_FUTURE_SKEW_SECONDS):
        raise PublicBootstrapIndexError(
            "public pointer authority was issued too far in the future"
        )
    if not allow_expired and now >= expires:
        raise PublicBootstrapIndexError("public pointer authority has expired")


def _validate_signature(value: Any, label: str) -> None:
    signature = _require_mapping(value, label)
    _require_exact_keys(signature, {"algorithm", "key_id", "value"}, label)
    encoded = signature.get("value")
    if (
        signature.get("algorithm") != "ed25519"
        or not _matches(signature.get("key_id"), _SAFE_ID)
        or not isinstance(encoded, str)
        or len(encoded) != 88
    ):
        raise PublicBootstrapIndexError(f"{label} is invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise PublicBootstrapIndexError(f"{label} is invalid") from None
    if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != encoded:
        raise PublicBootstrapIndexError(f"{label} is invalid")


def _trust_roles_overlap(
    *,
    authority_key_id: str,
    freshness_key_id: str,
    verifier: SignatureVerifier,
    freshness_verifier: SignatureVerifier,
    authority_signer: ReleaseSigner | None = None,
    freshness_signer: ReleaseSigner | None = None,
) -> bool:
    if authority_key_id == freshness_key_id:
        return True
    authority_fingerprint = _signer_fingerprint(authority_signer) or (
        _verifier_fingerprint(verifier, authority_key_id)
    )
    freshness_fingerprint = _signer_fingerprint(freshness_signer) or (
        _verifier_fingerprint(freshness_verifier, freshness_key_id)
    )
    return bool(
        authority_fingerprint
        and freshness_fingerprint
        and authority_fingerprint == freshness_fingerprint
    )


def _signer_fingerprint(signer: ReleaseSigner | None) -> str | None:
    if signer is None:
        return None
    try:
        material = signer.public_key_bytes  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(material, bytes) or len(material) != 32:
        return None
    return hashlib.sha256(material).hexdigest()


def _verifier_fingerprint(
    verifier: SignatureVerifier,
    key_id: str,
) -> str | None:
    resolver = getattr(verifier, "key_fingerprint", None)
    if not callable(resolver):
        return None
    try:
        fingerprint = resolver(key_id)
    except Exception:
        return None
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return None
    return fingerprint


def _validate_sources(
    value: Any,
    *,
    file_name: str,
    label: str,
) -> tuple[tuple[tuple[str, str, int], ...], tuple[str, ...]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise PublicBootstrapIndexError(f"{label} must contain one to three sources")
    expected_order = (
        ("github-cn-mirror", 0),
        ("github-release", 1),
        ("ecorex-cdn", 2),
    )
    identities: list[tuple[str, str, int]] = []
    bases: list[str] = []
    seen_ids: set[str] = set()
    suffix = "/" + quote(file_name, safe="")
    for position, source_value in enumerate(value):
        source = _require_mapping(source_value, f"{label}[{position}]")
        _require_exact_keys(
            source, {"source_id", "kind", "priority", "url"}, f"{label}[{position}]"
        )
        source_id = source.get("source_id")
        kind = source.get("kind")
        priority = source.get("priority")
        url = source.get("url")
        if (
            not _matches(source_id, _SAFE_ID)
            or source_id in seen_ids
            or (kind, priority) != expected_order[position]
            or not isinstance(url, str)
            or len(url.encode("utf-8")) > 2048
            or not url.endswith(suffix)
        ):
            raise PublicBootstrapIndexError(f"{label}[{position}] is invalid")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PublicBootstrapIndexError(f"{label}[{position}] is invalid")
        seen_ids.add(source_id)
        identities.append((source_id, kind, priority))
        bases.append(url[: -len(suffix)])
    return tuple(identities), tuple(bases)


def validate_publication_receipt(
    *,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    receipt: Mapping[str, Any],
    receipt_sha256: str,
) -> dict[str, dict[str, tuple[int, str, str]]]:
    canonical_receipt_sha256 = hashlib.sha256(
        _canonical_json_bytes(receipt)
    ).hexdigest()
    if canonical_receipt_sha256 != receipt_sha256:
        raise PublicBootstrapIndexError(
            "publication receipt digest differs from its canonical bytes"
        )
    schema_version = receipt.get("schema_version")
    if schema_version == 1:
        if (
            set(receipt) != _LEGACY_RECEIPT_KEYS
            or isinstance(receipt.get("github_release_id"), bool)
            or not isinstance(receipt.get("github_release_id"), int)
            or receipt["github_release_id"] < 1
            or receipt.get("github_draft") is not False
        ):
            raise PublicBootstrapIndexError("publication receipt shape is invalid")
        receipt_sources = tuple(manifest.sources)
    elif schema_version == 2:
        if (
            set(receipt) != _PRIMARY_ONLY_RECEIPT_KEYS
            or receipt.get("publication_policy") != publication_receipt_policy(manifest)
        ):
            raise PublicBootstrapIndexError("publication receipt shape is invalid")
        receipt_sources = required_publication_sources(manifest)
    else:
        raise PublicBootstrapIndexError("publication receipt shape is invalid")
    if (
        receipt.get("release_id") != manifest.release_id
        or receipt.get("version") != manifest.version
        or receipt.get("manifest_sha256") != manifest_sha256
    ):
        raise PublicBootstrapIndexError(
            "publication receipt does not describe the signed public release"
        )
    raw_sources = receipt.get("source_receipts")
    if not isinstance(raw_sources, Mapping):
        raise PublicBootstrapIndexError("publication receipt source set is invalid")
    source_ids = tuple(source.source_id for source in receipt_sources)
    if set(raw_sources) != set(source_ids):
        raise PublicBootstrapIndexError("publication receipt source set is incomplete")

    artifact_by_name = {artifact.file_name: artifact for artifact in manifest.artifacts}
    if len(artifact_by_name) != len(manifest.artifacts):
        raise PublicBootstrapIndexError("signed artifact filenames are not unique")
    expected_names = set(artifact_by_name).union(_RESERVED_RELEASE_FILES)
    common_identity: dict[str, tuple[int, str]] | None = None
    result: dict[str, dict[str, tuple[int, str, str]]] = {}
    for source in receipt_sources:
        raw_entries = raw_sources.get(source.source_id)
        if not isinstance(raw_entries, list) or len(raw_entries) != len(expected_names):
            raise PublicBootstrapIndexError(
                "publication receipt asset set is incomplete"
            )
        entries: dict[str, tuple[int, str, str]] = {}
        for raw_entry in raw_entries:
            if (
                not isinstance(raw_entry, Mapping)
                or set(raw_entry) != _RECEIPT_ASSET_KEYS
            ):
                raise PublicBootstrapIndexError(
                    "publication receipt contains an invalid asset"
                )
            name = raw_entry.get("name")
            size = raw_entry.get("size_bytes")
            sha256 = raw_entry.get("sha256")
            url = raw_entry.get("url")
            expected_url = (
                f"{source.base_url}/{quote(name, safe='')}"
                if isinstance(name, str)
                else None
            )
            if (
                not isinstance(name, str)
                or name not in expected_names
                or name in entries
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 1
                or not isinstance(sha256, str)
                or not _is_sha256(sha256)
                or url != expected_url
            ):
                raise PublicBootstrapIndexError(
                    "publication receipt contains an invalid asset"
                )
            artifact = artifact_by_name.get(name)
            if artifact is not None and (
                size != artifact.size_bytes or sha256 != artifact.sha256
            ):
                raise PublicBootstrapIndexError(
                    "publication receipt artifact identity differs from the manifest"
                )
            if name == "release-manifest.json" and sha256 != manifest_sha256:
                raise PublicBootstrapIndexError(
                    "publication receipt manifest digest differs"
                )
            entries[name] = (size, sha256, url)
        if set(entries) != expected_names:
            raise PublicBootstrapIndexError(
                "publication receipt asset set is incomplete"
            )
        identity = {name: (item[0], item[1]) for name, item in entries.items()}
        if common_identity is None:
            common_identity = identity
        elif identity != common_identity:
            raise PublicBootstrapIndexError(
                "publication origins do not contain identical release bytes"
            )
        result[source.source_id] = entries
    return result


# Backward-compatible internal name for existing callers. New trust-boundary
# code must use the public validator above rather than importing a private
# implementation detail.
_validate_publication_receipt = validate_publication_receipt


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise PublicBootstrapIndexError(
            "public index input is not canonical JSON"
        ) from None


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not _is_sha256(value):
        raise PublicBootstrapIndexError(f"{label} must be a lowercase SHA-256 digest")


def _require_safe_existing_file(path: Path) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        path.is_symlink()
        or bool(attributes & reparse_flag)
        or not stat_module.S_ISREG(metadata.st_mode)
    ):
        raise PublicBootstrapIndexError(
            "public index output must be a regular non-link file"
        )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
