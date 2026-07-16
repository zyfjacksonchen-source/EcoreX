"""Production Linux/aarch64 cloud artifact and detached-signing pipeline."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ecorex import __version__
from ecorex.deployment.cloud_artifact import (
    BUILD_CONTRACT,
    CLOUD_MANIFEST_SIGNING_DOMAIN,
    CloudArtifactBuildError,
    attach_cloud_artifact_signature,
    canonical_cloud_manifest,
    cloud_manifest_file_bytes,
    cloud_manifest_signing_payload,
    unsigned_cloud_manifest,
)


PYTHON_VERSION = (3, 11, 9)
ARCHITECTURE = "aarch64"
DESCRIPTOR_NAME = "cloud-unsigned-signature-descriptor.json"
MANIFEST_NAME = "cloud-release-manifest.json"
PAYLOAD_NAME = "cloud-release-manifest.signing-payload"
RECEIPT_NAME = "cloud-build-receipt.json"
SIGNATURE_RESPONSE_NAME = "cloud-manifest-signature-response.json"
MAX_SIGNING_PAYLOAD_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ENTRYPOINTS = {
    "ecorex-control-plane": "ecorex.control_plane.production",
    "ecorex-gateway": "ecorex.gateway.production",
    "ecorex-image": "ecorex.image_orchestrator.production",
}


class CloudArtifactPipelineError(RuntimeError):
    """Fail-closed error with a stable, non-secret diagnostic code."""


def build_linux_cloud_artifact(
    source_root: Path,
    artifact_root: Path,
    handoff_root: Path,
    *,
    release_id: str,
    expected_commit: str,
) -> dict[str, Any]:
    """Build an immutable application venv and an unsigned signing handoff.

    Dependencies are installed only from repository hash locks and only as
    wheels.  EcoreX itself is built as a real wheel from ``git archive`` of the
    exact clean main commit, then installed by distribution name (never with
    ``-e`` and never from the live repository path).
    """

    _require_linux_toolchain()
    source = _exact_clean_main(source_root, expected_commit)
    artifact = _new_directory_path(artifact_root, "cloud_artifact_output_exists")
    handoff = _new_directory_path(handoff_root, "cloud_handoff_output_exists")
    locks = _validated_locks(source)
    source_date_epoch = _git(source, "show", "-s", "--format=%ct", expected_commit)
    if not source_date_epoch.isdigit():
        raise CloudArtifactPipelineError("cloud_source_commit_time_invalid")

    created: list[Path] = []
    try:
        artifact.mkdir(parents=True, mode=0o755)
        created.append(artifact)
        handoff.mkdir(parents=True, mode=0o700)
        created.append(handoff)
        environment = _build_environment(source_date_epoch)
        with tempfile.TemporaryDirectory(prefix="ecorex-cloud-source-") as raw_source:
            archived_source = Path(raw_source) / "source"
            _extract_exact_source(source, archived_source, expected_commit)
            archived_locks = _validated_locks(archived_source)
            if archived_locks["manifest_sha256"] != locks["manifest_sha256"]:
                raise CloudArtifactPipelineError("cloud_dependency_lock_digest_mismatch")
            wheel = _build_runtime_tree(
                archived_source,
                source,
                artifact,
                locks=archived_locks,
                environment=environment,
            )
            _copy_deployment_templates(archived_source, artifact)
        _normalize_tree_modes(artifact)
        verification = _verify_runtime_tree(artifact, source)
        manifest = unsigned_cloud_manifest(
            artifact,
            release_id=release_id,
            source_commit=expected_commit,
            dependency_lock_manifest_sha256=locks["manifest_sha256"],
        )
        manifest_bytes = cloud_manifest_file_bytes(manifest)
        canonical = canonical_cloud_manifest(manifest)
        signing_payload = cloud_manifest_signing_payload(manifest)
        if len(signing_payload) > MAX_SIGNING_PAYLOAD_BYTES:
            raise CloudArtifactPipelineError("cloud_signing_payload_too_large")
        receipt = {
            "schema_version": 1,
            "contract": BUILD_CONTRACT,
            "release_id": release_id,
            "version": __version__,
            "source_commit": expected_commit,
            "source_tree_clean": True,
            "source_date_epoch": int(source_date_epoch),
            "platform": "linux",
            "architecture": ARCHITECTURE,
            "python_version": ".".join(str(part) for part in PYTHON_VERSION),
            "dependency_lock_manifest_sha256": locks["manifest_sha256"],
            "dependency_locks": {
                profile: {
                    "filename": Path(value["path"]).name,
                    "sha256": value["sha256"],
                }
                for profile, value in sorted(locks["profiles"].items())
            },
            "application_wheel": wheel,
            "artifact_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "canonical_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
            "signing_payload_sha256": hashlib.sha256(signing_payload).hexdigest(),
            "file_count": len(manifest["files"]),
            "total_bytes": sum(int(row["size_bytes"]) for row in manifest["files"]),
            "posix_mode_contract": {
                "allowed_file_modes": ["0644", "0755"],
                "required_executable_paths": sorted(
                    f"venv/bin/{name}" for name in _ENTRYPOINTS
                ),
            },
            "verification": verification,
        }
        descriptor = {
            "schema_version": 1,
            "contract": "ecorex.detached-cloud-manifest-signing.v1",
            "algorithm": "ed25519",
            "release_id": release_id,
            "version": __version__,
            "source_commit": expected_commit,
            "manifest_file": MANIFEST_NAME,
            "manifest_sha256": receipt["artifact_manifest_sha256"],
            "payload_file": PAYLOAD_NAME,
            "payload_format": "ecorex-domain-prefix-nul-plus-canonical-json",
            "payload_sha256": receipt["signing_payload_sha256"],
            "payload_size_bytes": len(signing_payload),
            "build_receipt_file": RECEIPT_NAME,
            "build_receipt_sha256": _digest_bytes(_json_bytes(receipt)),
        }
        _write_new(handoff / MANIFEST_NAME, manifest_bytes, mode=0o600)
        _write_new(handoff / PAYLOAD_NAME, signing_payload, mode=0o600)
        _write_new(handoff / RECEIPT_NAME, _json_bytes(receipt), mode=0o600)
        _write_new(handoff / DESCRIPTOR_NAME, _json_bytes(descriptor), mode=0o600)
        _fsync_directory(handoff)
        _fsync_directory(artifact)
        return {
            "schema_version": 1,
            "release_id": release_id,
            "source_commit": expected_commit,
            "artifact_root": str(artifact),
            "handoff_root": str(handoff),
            "manifest_sha256": receipt["artifact_manifest_sha256"],
            "signing_payload_sha256": receipt["signing_payload_sha256"],
            "file_count": receipt["file_count"],
            "total_bytes": receipt["total_bytes"],
        }
    except BaseException:
        for path in reversed(created):
            shutil.rmtree(path, ignore_errors=True)
        raise


def attach_detached_cloud_signature(
    artifact_root: Path,
    handoff_root: Path,
    signature_response_path: Path,
    release_keyring_path: Path,
) -> dict[str, Any]:
    """Attach a Windows-produced signature after Linux revalidation."""

    artifact = _existing_directory(artifact_root, "cloud_artifact_root_invalid")
    handoff = _existing_directory(handoff_root, "cloud_handoff_root_invalid")
    descriptor = _strict_object(handoff / DESCRIPTOR_NAME)
    receipt = _strict_object(handoff / RECEIPT_NAME)
    manifest = _strict_object(handoff / MANIFEST_NAME)
    payload = _read_regular(handoff / PAYLOAD_NAME, MAX_SIGNING_PAYLOAD_BYTES)
    response = _strict_object(signature_response_path)
    keyring = _strict_object(release_keyring_path)
    _validate_handoff(descriptor, receipt, manifest, payload)
    expected_response = {
        "schema_version",
        "contract",
        "algorithm",
        "key_id",
        "manifest_sha256",
        "payload_sha256",
        "signature_b64",
    }
    if (
        set(response) != expected_response
        or response.get("schema_version") != 1
        or response.get("contract") != "ecorex.detached-cloud-manifest-signature.v1"
        or response.get("algorithm") != "ed25519"
        or response.get("manifest_sha256") != descriptor["manifest_sha256"]
        or response.get("payload_sha256") != descriptor["payload_sha256"]
    ):
        raise CloudArtifactPipelineError("cloud_signature_response_invalid")
    key_id = response.get("key_id")
    encoded_key = keyring.get(key_id) if isinstance(key_id, str) else None
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
        signature = base64.b64decode(response.get("signature_b64"), validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except Exception:
        raise CloudArtifactPipelineError("cloud_signature_response_invalid") from None
    try:
        result = attach_cloud_artifact_signature(
            artifact,
            manifest=manifest,
            key_id=key_id,
            signature=signature,
            public_key=public_key,
        )
    except CloudArtifactBuildError as exc:
        raise CloudArtifactPipelineError(str(exc)) from None
    _verify_manifest_modes(artifact, manifest)
    return result


def create_detached_signature_response(
    descriptor_path: Path,
    payload_path: Path,
    *,
    key_id: str,
    signature: bytes,
) -> dict[str, Any]:
    """Validate signer inputs and return the narrow Windows response value."""

    descriptor, payload = read_detached_signing_payload(descriptor_path, payload_path)
    return create_detached_signature_response_from_payload(
        descriptor, payload, key_id=key_id, signature=signature
    )


def read_detached_signing_payload(
    descriptor_path: Path, payload_path: Path
) -> tuple[dict[str, Any], bytes]:
    """Read and bind the exact canonical bytes presented to Windows DPAPI."""

    descriptor = _strict_object(descriptor_path)
    payload = _read_regular(payload_path, MAX_SIGNING_PAYLOAD_BYTES)
    _validate_unsigned_descriptor(descriptor, payload)
    return descriptor, payload


def create_detached_signature_response_from_payload(
    descriptor: Mapping[str, Any],
    payload: bytes,
    *,
    key_id: str,
    signature: bytes,
) -> dict[str, Any]:
    """Create a response for bytes already read through the bounded reader."""

    _validate_unsigned_descriptor(descriptor, payload)
    if not isinstance(key_id, str) or not key_id or not isinstance(signature, bytes) or len(signature) != 64:
        raise CloudArtifactPipelineError("cloud_unsigned_descriptor_invalid")
    return {
        "schema_version": 1,
        "contract": "ecorex.detached-cloud-manifest-signature.v1",
        "algorithm": "ed25519",
        "key_id": key_id,
        "manifest_sha256": descriptor["manifest_sha256"],
        "payload_sha256": descriptor["payload_sha256"],
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }


def _validate_unsigned_descriptor(
    descriptor: Mapping[str, Any], payload: bytes
) -> None:
    try:
        canonical = payload.removeprefix(CLOUD_MANIFEST_SIGNING_DOMAIN)
        if len(canonical) == len(payload):
            raise ValueError
        manifest = json.loads(canonical, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise CloudArtifactPipelineError("cloud_unsigned_descriptor_invalid") from None
    manifest_fields = {
        "schema_version",
        "release_id",
        "version",
        "platform",
        "architecture",
        "python_version",
        "build_contract",
        "source_commit",
        "dependency_lock_manifest_sha256",
        "files",
    }
    manifest_valid = (
        isinstance(manifest, dict)
        and set(manifest) == manifest_fields
        and manifest.get("schema_version") == 1
        and manifest.get("version") == "1.0.0"
        and manifest.get("platform") == "linux"
        and manifest.get("architecture") == ARCHITECTURE
        and manifest.get("python_version") == "3.11.9"
        and manifest.get("build_contract") == BUILD_CONTRACT
        and _COMMIT.fullmatch(str(manifest.get("source_commit"))) is not None
        and _SHA.fullmatch(
            str(manifest.get("dependency_lock_manifest_sha256"))
        )
        is not None
        and isinstance(manifest.get("files"), list)
        and bool(manifest.get("files"))
        and canonical == canonical_cloud_manifest(manifest)
    )
    if (
        not manifest_valid
        or set(descriptor)
        != {
            "schema_version",
            "contract",
            "algorithm",
            "release_id",
            "version",
            "source_commit",
            "manifest_file",
            "manifest_sha256",
            "payload_file",
            "payload_format",
            "payload_sha256",
            "payload_size_bytes",
            "build_receipt_file",
            "build_receipt_sha256",
        }
        or descriptor.get("schema_version") != 1
        or descriptor.get("contract") != "ecorex.detached-cloud-manifest-signing.v1"
        or descriptor.get("algorithm") != "ed25519"
        or descriptor.get("payload_file") != PAYLOAD_NAME
        or descriptor.get("payload_format")
        != "ecorex-domain-prefix-nul-plus-canonical-json"
        or descriptor.get("payload_size_bytes") != len(payload)
        or descriptor.get("payload_sha256") != _digest_bytes(payload)
        or descriptor.get("manifest_sha256")
        != _digest_bytes(cloud_manifest_file_bytes(manifest))
        or descriptor.get("release_id") != manifest.get("release_id")
        or descriptor.get("version") != manifest.get("version")
        or descriptor.get("source_commit") != manifest.get("source_commit")
        or _SHA.fullmatch(str(descriptor.get("build_receipt_sha256"))) is None
    ):
        raise CloudArtifactPipelineError("cloud_unsigned_descriptor_invalid")


def _require_linux_toolchain() -> None:
    machine = platform.machine().casefold()
    if (
        sys.platform != "linux"
        or machine not in {"aarch64", "arm64"}
        or sys.version_info[:3] != PYTHON_VERSION
    ):
        raise CloudArtifactPipelineError("cloud_linux_aarch64_python_3_11_9_required")


def _exact_clean_main(root: Path, commit: str) -> Path:
    source = _existing_directory(root, "cloud_source_root_invalid")
    if _COMMIT.fullmatch(commit) is None:
        raise CloudArtifactPipelineError("cloud_source_commit_invalid")
    head = _git(source, "rev-parse", "HEAD")
    remote = _git(source, "rev-parse", "origin/main")
    status = _git(source, "status", "--porcelain", "--untracked-files=all").splitlines()
    dirty = [
        row
        for row in status
        if not row[3:].replace("\\", "/").startswith(".artifacts/")
    ]
    if head != commit or remote != commit or dirty:
        raise CloudArtifactPipelineError("cloud_exact_clean_main_required")
    return source


def _validated_locks(source: Path) -> dict[str, Any]:
    lock_root = source / "requirements" / "locks"
    manifest_path = lock_root / "manifest.json"
    manifest_bytes = _read_regular(manifest_path, 1024 * 1024)
    try:
        value = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError):
        raise CloudArtifactPipelineError("cloud_dependency_lock_manifest_invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("lock_type") != "ecorex-python-hash-lock-set"
        or value.get("python") != "3.11.9"
        or not isinstance(value.get("profiles"), list)
    ):
        raise CloudArtifactPipelineError("cloud_dependency_lock_manifest_invalid")
    wanted = {"bootstrap", "cloud"}
    profiles: dict[str, dict[str, str]] = {}
    for row in value["profiles"]:
        if not isinstance(row, dict) or row.get("profile") not in wanted:
            continue
        profile = row["profile"]
        filename = row.get("lock")
        expected = row.get("lock_sha256")
        if (
            set(row)
            != {"input", "input_sha256", "lock", "lock_sha256", "profile"}
            or filename != f"{profile}.lock"
            or _SHA.fullmatch(str(expected)) is None
            or profile in profiles
        ):
            raise CloudArtifactPipelineError("cloud_dependency_lock_manifest_invalid")
        lock_path = lock_root / filename
        if _digest_bytes(_read_regular(lock_path, 4 * 1024 * 1024)) != expected:
            raise CloudArtifactPipelineError("cloud_dependency_lock_digest_mismatch")
        profiles[profile] = {"path": str(lock_path), "sha256": expected}
    if set(profiles) != wanted:
        raise CloudArtifactPipelineError("cloud_dependency_lock_profile_missing")
    return {
        "manifest_sha256": _digest_bytes(manifest_bytes),
        "profiles": profiles,
    }


def _extract_exact_source(source: Path, destination: Path, commit: str) -> None:
    destination.mkdir(mode=0o700)
    archive_path = destination.parent / "source.tar"
    try:
        with archive_path.open("xb") as stream:
            result = subprocess.run(
                ("git", "archive", "--format=tar", commit),
                cwd=source,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
            )
        if result.returncode != 0:
            raise CloudArtifactPipelineError("cloud_source_archive_failed")
        with tarfile.open(archive_path, "r:") as archive:
            for member in archive.getmembers():
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or not (member.isdir() or member.isfile())
                ):
                    raise CloudArtifactPipelineError("cloud_source_archive_invalid")
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError):
        raise CloudArtifactPipelineError("cloud_source_archive_failed") from None
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass


def _build_runtime_tree(
    archived_source: Path,
    source: Path,
    artifact: Path,
    *,
    locks: Mapping[str, Any],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    venv = artifact / "venv"
    _run((sys.executable, "-m", "venv", "--copies", str(venv)), cwd=artifact, env=environment)
    python = venv / "bin" / "python3.11"
    if not python.is_file() or python.is_symlink():
        raise CloudArtifactPipelineError("cloud_venv_python_not_self_contained")
    lib64 = venv / "lib64"
    if lib64.is_symlink():
        lib64.unlink()
    for profile in ("bootstrap", "cloud"):
        _run(
            (
                str(python),
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-deps",
                "--no-cache-dir",
                "--no-input",
                "-r",
                locks["profiles"][profile]["path"],
                "--index-url",
                "https://pypi.org/simple",
            ),
            cwd=artifact,
            env=environment,
        )
    with tempfile.TemporaryDirectory(prefix="ecorex-cloud-wheel-") as raw_wheels:
        wheels = Path(raw_wheels)
        _run(
            (
                str(python),
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-cache-dir",
                "--wheel-dir",
                str(wheels),
                str(archived_source),
            ),
            cwd=archived_source,
            env=environment,
        )
        candidates = sorted(wheels.glob("ecorex_agent_runtime-1.0.0-*.whl"))
        if len(candidates) != 1:
            raise CloudArtifactPipelineError("cloud_application_wheel_invalid")
        wheel = candidates[0]
        wheel_value = {
            "filename": wheel.name,
            "sha256": _digest_bytes(_read_regular(wheel, 256 * 1024 * 1024)),
            "size_bytes": wheel.stat().st_size,
        }
        _run(
            (
                str(python),
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                "--no-input",
                "--find-links",
                str(wheels),
                "ecorex-agent-runtime==1.0.0",
            ),
            cwd=artifact,
            env=environment,
        )
    _write_relocatable_entrypoints(venv / "bin")
    return wheel_value


def _copy_deployment_templates(source: Path, artifact: Path) -> None:
    origin = source / "deploy" / "ecorex-cloud-sidecar"
    destination = artifact / "deployment"
    if origin.is_symlink() or not origin.is_dir():
        raise CloudArtifactPipelineError("cloud_deployment_templates_missing")
    shutil.copytree(origin, destination, symlinks=False)


def _write_relocatable_entrypoints(bin_root: Path) -> None:
    for name, module in _ENTRYPOINTS.items():
        path = bin_root / name
        if path.is_symlink() or not path.is_file():
            raise CloudArtifactPipelineError("cloud_console_script_missing")
        payload = (
            "#!/bin/sh\n"
            'SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 126\n'
            f'exec "$SELF_DIR/python3.11" -m {module} "$@"\n'
        ).encode("utf-8")
        path.write_bytes(payload)
        os.chmod(path, 0o755)


def _normalize_tree_modes(root: Path) -> None:
    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        os.chmod(base, 0o755)
        for name in directories:
            path = base / name
            if path.is_symlink():
                raise CloudArtifactPipelineError("cloud_artifact_symlink_forbidden")
            os.chmod(path, 0o755)
        for name in filenames:
            path = base / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise CloudArtifactPipelineError("cloud_artifact_file_invalid")
            executable = bool(stat.S_IMODE(metadata.st_mode) & 0o111)
            os.chmod(path, 0o755 if executable else 0o644)


def _verify_runtime_tree(artifact: Path, source: Path) -> dict[str, Any]:
    python = artifact / "venv" / "bin" / "python3.11"
    probe = (
        "import importlib.resources, json, pathlib, sys; "
        "import ecorex, ecorex.control_plane.production, ecorex.gateway.production, "
        "ecorex.image_orchestrator.production; "
        "asset=importlib.resources.files('ecorex.control_plane.admin_web').joinpath('static/index.html'); "
        "print(json.dumps({'version':ecorex.__version__,'prefix':sys.prefix,"
        "'package':str(pathlib.Path(ecorex.__file__).resolve()),'admin_asset':asset.is_file()}))"
    )
    result = _run((str(python), "-I", "-c", probe), cwd=artifact, capture=True)
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise CloudArtifactPipelineError("cloud_runtime_import_probe_invalid") from None
    artifact_real = artifact.resolve(strict=True)
    try:
        package = Path(value["package"]).resolve(strict=True)
        prefix = Path(value["prefix"]).resolve(strict=True)
        package.relative_to(artifact_real)
        prefix.relative_to(artifact_real)
    except (KeyError, OSError, ValueError, TypeError):
        raise CloudArtifactPipelineError("cloud_runtime_import_escaped_artifact") from None
    if value.get("version") != "1.0.0" or value.get("admin_asset") is not True:
        raise CloudArtifactPipelineError("cloud_runtime_package_data_missing")
    forbidden = {str(source.resolve(strict=True)), "__editable__", "editable_finder"}
    pth_files = sorted((artifact / "venv").rglob("*.pth"))
    for pth in pth_files:
        text = _read_regular(pth, 1024 * 1024).decode("utf-8", errors="strict")
        folded = text.casefold()
        if any(marker.casefold() in folded for marker in forbidden):
            raise CloudArtifactPipelineError("cloud_repository_pth_forbidden")
    for name in _ENTRYPOINTS:
        script = artifact / "venv" / "bin" / name
        _run((str(script), "--help"), cwd=artifact, env=_build_environment("0"))
    return {
        "imports_from_artifact": True,
        "admin_package_data": True,
        "console_scripts": sorted(_ENTRYPOINTS),
        "repository_pth_absent": True,
    }


def _validate_handoff(
    descriptor: Mapping[str, Any],
    receipt: Mapping[str, Any],
    manifest: Mapping[str, Any],
    payload: bytes,
) -> None:
    manifest_bytes = cloud_manifest_file_bytes(dict(manifest))
    receipt_fields = {
        "schema_version",
        "contract",
        "release_id",
        "version",
        "source_commit",
        "source_tree_clean",
        "source_date_epoch",
        "platform",
        "architecture",
        "python_version",
        "dependency_lock_manifest_sha256",
        "dependency_locks",
        "application_wheel",
        "artifact_manifest_sha256",
        "canonical_manifest_sha256",
        "signing_payload_sha256",
        "file_count",
        "total_bytes",
        "posix_mode_contract",
        "verification",
    }
    if (
        not isinstance(descriptor, dict)
        or not isinstance(receipt, dict)
        or set(receipt) != receipt_fields
        or receipt.get("schema_version") != 1
        or receipt.get("contract") != BUILD_CONTRACT
        or receipt.get("source_tree_clean") is not True
        or receipt.get("source_commit") != manifest.get("source_commit")
        or receipt.get("dependency_lock_manifest_sha256")
        != manifest.get("dependency_lock_manifest_sha256")
        or descriptor.get("source_commit") != manifest.get("source_commit")
        or descriptor.get("release_id") != manifest.get("release_id")
        or descriptor.get("version") != manifest.get("version")
        or descriptor.get("contract") != "ecorex.detached-cloud-manifest-signing.v1"
        or _descriptor_invalid(descriptor, payload)
        or descriptor.get("manifest_sha256") != _digest_bytes(manifest_bytes)
        or descriptor.get("build_receipt_sha256") != _digest_bytes(_json_bytes(dict(receipt)))
        or payload != cloud_manifest_signing_payload(dict(manifest))
        or receipt.get("artifact_manifest_sha256") != descriptor.get("manifest_sha256")
        or receipt.get("signing_payload_sha256") != descriptor.get("payload_sha256")
        or receipt.get("release_id") != manifest.get("release_id")
    ):
        raise CloudArtifactPipelineError("cloud_signing_handoff_invalid")


def _descriptor_invalid(descriptor: Mapping[str, Any], payload: bytes) -> bool:
    try:
        _validate_unsigned_descriptor(descriptor, payload)
    except CloudArtifactPipelineError:
        return True
    return False


def _verify_manifest_modes(artifact: Path, manifest: Mapping[str, Any]) -> None:
    for row in manifest.get("files", []):
        if not isinstance(row, Mapping):
            raise CloudArtifactPipelineError("cloud_manifest_mode_invalid")
        relative = row.get("path")
        expected = row.get("posix_mode")
        if not isinstance(relative, str) or expected not in {"0644", "0755"}:
            raise CloudArtifactPipelineError("cloud_manifest_mode_invalid")
        path = artifact.joinpath(*PurePosixPath(relative).parts)
        actual = stat.S_IMODE(path.lstat().st_mode)
        if os.name == "nt":
            actual = 0o755 if relative.startswith("venv/bin/") else 0o644
        if f"{actual:04o}" != expected:
            raise CloudArtifactPipelineError("cloud_artifact_mode_mismatch")


def _build_environment(source_date_epoch: str) -> dict[str, str]:
    value = dict(os.environ)
    value.update(
        {
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "SOURCE_DATE_EPOCH": source_date_epoch,
        }
    )
    return value


def _run(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CloudArtifactPipelineError("cloud_build_process_failed") from None
    if result.returncode != 0:
        raise CloudArtifactPipelineError("cloud_build_process_failed")
    return result


def _git(root: Path, *arguments: str) -> str:
    result = _run(("git", *arguments), cwd=root, capture=True)
    return result.stdout.strip()


def _new_directory_path(path: Path, code: str) -> Path:
    if not isinstance(path, Path):
        raise CloudArtifactPipelineError(code)
    value = path.absolute()
    if os.path.lexists(value):
        raise CloudArtifactPipelineError(code)
    parent = value.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise OSError
        resolved_parent = parent.resolve(strict=True)
    except OSError:
        raise CloudArtifactPipelineError(code) from None
    if os.path.normcase(str(parent)) != os.path.normcase(str(resolved_parent)):
        raise CloudArtifactPipelineError(code)
    return value


def _existing_directory(path: Path, code: str) -> Path:
    try:
        raw = path.absolute()
        metadata = raw.lstat()
        resolved = raw.resolve(strict=True)
    except (AttributeError, OSError):
        raise CloudArtifactPipelineError(code) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or raw != resolved:
        raise CloudArtifactPipelineError(code)
    return resolved


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= maximum
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError:
        raise CloudArtifactPipelineError("cloud_evidence_file_invalid") from None
    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if (
        len(payload) != metadata.st_size
        or identity(opened) != identity(metadata)
        or identity(after) != identity(metadata)
    ):
        raise CloudArtifactPipelineError("cloud_evidence_file_changed")
    return payload


def _strict_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_regular(path, 32 * 1024 * 1024), object_pairs_hook=_unique_object
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise CloudArtifactPipelineError("cloud_evidence_json_invalid") from None
    if not isinstance(value, dict):
        raise CloudArtifactPipelineError("cloud_evidence_json_invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _write_new(path: Path, payload: bytes, *, mode: int) -> None:
    try:
        with path.open("xb") as stream:
            os.chmod(path, mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CloudArtifactPipelineError("cloud_evidence_write_failed") from None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CloudArtifactPipelineError",
    "DESCRIPTOR_NAME",
    "MANIFEST_NAME",
    "MAX_SIGNING_PAYLOAD_BYTES",
    "PAYLOAD_NAME",
    "RECEIPT_NAME",
    "SIGNATURE_RESPONSE_NAME",
    "attach_detached_cloud_signature",
    "build_linux_cloud_artifact",
    "create_detached_signature_response",
    "create_detached_signature_response_from_payload",
    "read_detached_signing_payload",
]
