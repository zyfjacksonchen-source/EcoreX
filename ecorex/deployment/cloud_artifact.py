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
) -> dict[str, Any]:
    tree = _root(root)
    if __version__ != "1.0.0" or _RELEASE_ID.fullmatch(release_id) is None:
        raise CloudArtifactBuildError("cloud_artifact_identity_invalid")
    if any(os.path.lexists(tree / name) for name in _RESERVED):
        raise CloudArtifactBuildError("cloud_artifact_manifest_exists")
    files = _scan(tree)
    observed = {str(item["path"]) for item in files}
    if not _REQUIRED.issubset(observed):
        raise CloudArtifactBuildError("cloud_artifact_entrypoint_missing")
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "version": __version__,
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
        "files": files,
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    signature = signer.sign(canonical_cloud_manifest(manifest))
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


def _scan(root: Path) -> list[dict[str, Any]]:
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
                    or not 1 <= metadata.st_size <= MAX_FILE_BYTES
                ):
                    raise CloudArtifactBuildError("cloud_artifact_file_invalid")
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


__all__ = [
    "CloudArtifactBuildError",
    "build_signed_cloud_artifact",
    "canonical_cloud_manifest",
]
