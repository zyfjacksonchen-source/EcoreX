"""Deterministic signed manifest builder for the Linux cloud sidecar tree."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Any

from ecorex import __version__
from ecorex.release.signing import ReleaseSigner


MAX_FILES = 100_000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_RESERVED = {"cloud-release-manifest.json", "cloud-release-manifest.sig.json"}
_POSIX_MODES = frozenset({0o644, 0o755})
BUILD_CONTRACT = "ecorex.linux-aarch64-cloud-build.v1"
CLOUD_MANIFEST_SIGNING_DOMAIN = b"ecorex.cloud-release-manifest.v1\0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED = {
    "venv/bin/python3.11",
    "venv/bin/ecorex-control-plane",
    "venv/bin/ecorex-gateway",
    "venv/bin/ecorex-image",
    "deployment/systemd/ecorex-control-plane@.service",
    "deployment/systemd/ecorex-gateway@.service",
    "deployment/systemd/ecorex-image-api@.service",
    "deployment/systemd/ecorex-image-worker@.service",
    "deployment/nginx/control-plane-blue.conf",
    "deployment/nginx/control-plane-green.conf",
    "deployment/nginx/control-plane-disabled.conf",
    "deployment/nginx/admin-route-control-plane.conf",
    "deployment/nginx/ecorex-cloud.routes.conf",
}


class CloudArtifactBuildError(RuntimeError):
    pass


def canonical_cloud_manifest(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CloudArtifactBuildError("cloud_artifact_manifest_invalid") from None


def build_signed_cloud_artifact(
    root: Path,
    *,
    release_id: str,
    signer: ReleaseSigner,
    source_commit: str,
    dependency_lock_manifest_sha256: str,
) -> dict[str, Any]:
    tree = _root(root)
    manifest = unsigned_cloud_manifest(
        tree,
        release_id=release_id,
        source_commit=source_commit,
        dependency_lock_manifest_sha256=dependency_lock_manifest_sha256,
    )
    files = manifest["files"]
    manifest_bytes = cloud_manifest_file_bytes(manifest)
    signature = signer.sign(cloud_manifest_signing_payload(manifest))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise CloudArtifactBuildError("cloud_artifact_signature_invalid")
    signature_value = {
        "key_id": signer.key_id,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    signature_bytes = (
        json.dumps(signature_value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    _write_new(tree / "cloud-release-manifest.json", manifest_bytes)
    try:
        _write_new(tree / "cloud-release-manifest.sig.json", signature_bytes)
    except BaseException:
        try:
            (tree / "cloud-release-manifest.json").unlink()
        except OSError:
            pass
        raise
    return {
        "schema_version": 1,
        "release_id": release_id,
        "file_count": len(files),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
        "manifest_sha256": signature_value["manifest_sha256"],
        "key_id": signer.key_id,
    }


def unsigned_cloud_manifest(
    root: Path,
    *,
    release_id: str,
    source_commit: str,
    dependency_lock_manifest_sha256: str,
) -> dict[str, Any]:
    """Return the exact manifest that an offline release signer must sign.

    The manifest binds both bytes and normalized POSIX modes.  This matters for
    console entry points and deployment helper programs: a digest-only contract
    can otherwise install a valid script that the target kernel cannot execute.
    """

    tree = _root(root)
    if (
        __version__ != "1.0.0"
        or _RELEASE_ID.fullmatch(release_id) is None
        or _COMMIT.fullmatch(source_commit) is None
        or _SHA256.fullmatch(dependency_lock_manifest_sha256) is None
    ):
        raise CloudArtifactBuildError("cloud_artifact_identity_invalid")
    if any(os.path.lexists(tree / name) for name in _RESERVED):
        raise CloudArtifactBuildError("cloud_artifact_manifest_exists")
    files = scan_cloud_artifact_tree(tree)
    observed = {str(item["path"]) for item in files}
    if not _REQUIRED.issubset(observed):
        raise CloudArtifactBuildError("cloud_artifact_entrypoint_missing")
    by_path = {str(item["path"]): item for item in files}
    executable = {
        "venv/bin/python3.11",
        "venv/bin/ecorex-control-plane",
        "venv/bin/ecorex-gateway",
        "venv/bin/ecorex-image",
    }
    if any(by_path[path].get("posix_mode") != "0755" for path in executable):
        raise CloudArtifactBuildError("cloud_artifact_entrypoint_not_executable")
    return {
        "schema_version": 1,
        "release_id": release_id,
        "version": __version__,
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
        "build_contract": BUILD_CONTRACT,
        "source_commit": source_commit,
        "dependency_lock_manifest_sha256": dependency_lock_manifest_sha256,
        "files": files,
    }


def cloud_manifest_file_bytes(manifest: dict[str, Any]) -> bytes:
    """Return the durable manifest representation (the signature omits LF)."""

    return canonical_cloud_manifest(manifest) + b"\n"


def cloud_manifest_signing_payload(manifest: dict[str, Any]) -> bytes:
    """Domain-separated bytes accepted by the cloud release key."""

    return CLOUD_MANIFEST_SIGNING_DOMAIN + canonical_cloud_manifest(manifest)


def attach_cloud_artifact_signature(
    root: Path,
    *,
    manifest: dict[str, Any],
    key_id: str,
    signature: bytes,
    public_key: bytes,
) -> dict[str, Any]:
    """Verify and atomically attach an externally produced signature.

    The Linux side re-scans the tree immediately before attaching.  Windows is
    therefore only a byte signer; it is never trusted to describe Linux modes
    or artifact contents and never receives or exports private key material.
    """

    tree = _root(root)
    if not isinstance(manifest, dict) or manifest != unsigned_cloud_manifest(
        tree,
        release_id=str(manifest.get("release_id", "")),
        source_commit=str(manifest.get("source_commit", "")),
        dependency_lock_manifest_sha256=str(
            manifest.get("dependency_lock_manifest_sha256", "")
        ),
    ):
        raise CloudArtifactBuildError("cloud_artifact_manifest_changed")
    if (
        not isinstance(key_id, str)
        or not key_id
        or not isinstance(signature, bytes)
        or len(signature) != 64
        or not isinstance(public_key, bytes)
        or len(public_key) != 32
    ):
        raise CloudArtifactBuildError("cloud_artifact_signature_invalid")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, cloud_manifest_signing_payload(manifest)
        )
    except Exception:
        raise CloudArtifactBuildError("cloud_artifact_signature_invalid") from None
    manifest_bytes = cloud_manifest_file_bytes(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    signature_value = {
        "key_id": key_id,
        "manifest_sha256": manifest_sha256,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    signature_bytes = (
        json.dumps(signature_value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    _write_new(tree / "cloud-release-manifest.json", manifest_bytes)
    try:
        _write_new(tree / "cloud-release-manifest.sig.json", signature_bytes)
    except BaseException:
        try:
            (tree / "cloud-release-manifest.json").unlink()
        except OSError:
            pass
        raise
    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "file_count": len(manifest["files"]),
        "total_bytes": sum(int(item["size_bytes"]) for item in manifest["files"]),
        "manifest_sha256": manifest_sha256,
        "key_id": key_id,
    }


def _root(value: Path) -> Path:
    if not isinstance(value, Path):
        raise CloudArtifactBuildError("cloud_artifact_root_invalid")
    try:
        raw = value.absolute()
        metadata = raw.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        resolved = raw.resolve(strict=True)
    except OSError:
        raise CloudArtifactBuildError("cloud_artifact_root_invalid") from None
    if os.path.normcase(str(raw)) != os.path.normcase(str(resolved)):
        raise CloudArtifactBuildError("cloud_artifact_root_invalid")
    return resolved


def scan_cloud_artifact_tree(root: Path) -> list[dict[str, Any]]:
    root = _root(root)
    rows: list[dict[str, Any]] = []
    total = 0
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            base = Path(current)
            directories.sort()
            filenames.sort()
            for name in directories:
                metadata = (base / name).lstat()
                if stat.S_ISLNK(metadata.st_mode) or bool(
                    getattr(metadata, "st_file_attributes", 0) & reparse
                ):
                    raise CloudArtifactBuildError("cloud_artifact_link_forbidden")
            for name in filenames:
                path = base / name
                relative = path.relative_to(root).as_posix()
                if relative in _RESERVED:
                    raise CloudArtifactBuildError("cloud_artifact_manifest_exists")
                pure = PurePosixPath(relative)
                metadata = path.lstat()
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or stat.S_ISLNK(metadata.st_mode)
                    or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
                    or not stat.S_ISREG(metadata.st_mode)
                    # Python distributions may intentionally contain empty
                    # ``__init__.py`` or typing marker files.  They still
                    # need manifest coverage; rejecting them made a valid
                    # Linux venv impossible to package on the real target.
                    or not 0 <= metadata.st_size <= MAX_FILE_BYTES
                ):
                    raise CloudArtifactBuildError("cloud_artifact_file_invalid")
                mode = _portable_posix_mode(relative, metadata)
                if mode not in _POSIX_MODES:
                    raise CloudArtifactBuildError("cloud_artifact_mode_invalid")
                total += metadata.st_size
                if len(rows) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise CloudArtifactBuildError("cloud_artifact_size_limit")
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if _identity(opened) != _identity(metadata):
                        raise CloudArtifactBuildError("cloud_artifact_file_changed")
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                if _identity(after) != _identity(metadata):
                    raise CloudArtifactBuildError("cloud_artifact_file_changed")
                rows.append(
                    {
                        "path": relative,
                        "sha256": digest.hexdigest(),
                        "size_bytes": metadata.st_size,
                        "posix_mode": f"{mode:04o}",
                    }
                )
    except CloudArtifactBuildError:
        raise
    except OSError:
        raise CloudArtifactBuildError("cloud_artifact_scan_failed") from None
    if not rows:
        raise CloudArtifactBuildError("cloud_artifact_empty")
    return rows


def _write_new(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise CloudArtifactBuildError("cloud_artifact_manifest_exists") from None
    except OSError:
        raise CloudArtifactBuildError("cloud_artifact_manifest_write_failed") from None


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _portable_posix_mode(relative: str, metadata: os.stat_result) -> int:
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name == "nt":
        # Windows cannot carry Unix execute bits.  This branch exists only for
        # contract unit tests and the legacy detached-signing verifier; the
        # production builder itself is Linux/aarch64-only.
        executable = relative.startswith("venv/bin/") or relative.startswith(
            "deployment/signers/"
        )
        mode = 0o755 if executable else 0o644
    return mode


__all__ = [
    "CloudArtifactBuildError",
    "attach_cloud_artifact_signature",
    "build_signed_cloud_artifact",
    "BUILD_CONTRACT",
    "CLOUD_MANIFEST_SIGNING_DOMAIN",
    "cloud_manifest_file_bytes",
    "cloud_manifest_signing_payload",
    "canonical_cloud_manifest",
    "scan_cloud_artifact_tree",
    "unsigned_cloud_manifest",
]
