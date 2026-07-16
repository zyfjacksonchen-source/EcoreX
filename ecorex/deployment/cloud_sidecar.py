"""Single-host, side-by-side deployment authority for v1 cloud services.

The default operation is a read-only plan.  Mutation is possible only on the
exact Alibaba Cloud Linux 4/aarch64 machine whose machine-id digest is pinned
in the operator spec and repeated on the command line.  This module does not
contain SSH support, package installation, or secret values.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:  # The read-only planner is intentionally importable on release workstations.
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI import coverage
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
PYTHON_VERSION = "3.11.9"
PRODUCT_VERSION = "1.0.0"
TARGET_OS_ID = "alinux"
TARGET_OS_VERSION = "4"
TARGET_ARCHITECTURE = "aarch64"

INSTALL_ROOT = Path("/opt/ecorex/cloud")
RELEASE_ROOT = INSTALL_ROOT / "releases"
SLOT_ROOT = INSTALL_ROOT / "slots"
STATE_ROOT = Path("/var/lib/ecorex/cloud-deploy")
CONFIG_ROOT = Path("/etc/ecorex/cloud")
ENCRYPTED_VOLUME_ROOT = Path("/var/lib/ecorex")
SECRET_ROOT = ENCRYPTED_VOLUME_ROOT / "secrets"
SYSTEMD_ROOT = Path("/etc/systemd/system")
NGINX_ROOT = Path("/etc/nginx/ecorex-cloud")
NGINX_SERVER_CONFIG = Path("/etc/nginx/conf.d/ecorex-mvdcm.conf")
LOCK_PATH = Path("/run/lock/ecorex-cloud-deploy.lock")

NGINX_ROUTE_INCLUDE = "include /etc/nginx/ecorex-cloud/ecorex-cloud.routes.conf;"
LEGACY_ADMIN_LOCATION_HEADERS = (
    "location = /ecorex-agent/admin",
    "location ^~ /ecorex-agent/admin/api/",
    "location ^~ /ecorex-agent/api/admin/",
    "location ^~ /ecorex-agent/admin/",
)

SLOTS = ("blue", "green")
PORTS = {
    "blue": {"control_plane": 18771, "gateway": 18772, "image": 18773},
    "green": {"control_plane": 18871, "gateway": 18872, "image": 18873},
}
SERVICE_NAMES = (
    "ecorex-control-plane",
    "ecorex-gateway",
    "ecorex-image-api",
    "ecorex-image-worker",
)
ENV_NAMES = {
    "control-plane": ("control-plane.env", "control-plane.secret.env"),
    "gateway": ("gateway.env", "gateway.secret.env"),
    "image": ("image.env", "image.secret.env"),
}
SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}\Z")
SAFE_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CloudDeployError(RuntimeError):
    """Redacted deployment failure safe to show to an operator."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{3,80}", code):
            code = "deployment_failed"
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class CloudDeploymentSpec:
    release_id: str
    artifact_root: Path
    artifact_manifest_sha256: str
    release_keyring_path: Path
    release_keyring_sha256: str
    target_machine_id_sha256: str
    encryption_attestation_path: Path
    encryption_attestation_sha256: str
    python_binary: Path = Path("/opt/ecorex/platform/python-3.11.9/bin/python3.11")
    postgres_binary: Path = Path("/usr/bin/psql")
    minio_binary: Path = Path("/opt/ecorex/platform/minio/minio")
    nginx_binary: Path = Path("/usr/sbin/nginx")
    nginx_server_config: Path = NGINX_SERVER_CONFIG
    systemctl_binary: Path = Path("/usr/bin/systemctl")

    @classmethod
    def from_json(cls, path: Path) -> "CloudDeploymentSpec":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CloudDeployError("deployment_spec_invalid") from None
        if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
            raise CloudDeployError("deployment_spec_invalid")
        expected = {
            "schema_version",
            "release_id",
            "artifact_root",
            "artifact_manifest_sha256",
            "release_keyring_path",
            "release_keyring_sha256",
            "target_machine_id_sha256",
            "encryption_attestation_path",
            "encryption_attestation_sha256",
            "python_binary",
            "postgres_binary",
            "minio_binary",
            "nginx_binary",
            "nginx_server_config",
            "systemctl_binary",
        }
        if set(raw) - expected:
            raise CloudDeployError("deployment_spec_unknown_field")
        try:
            spec = cls(
                release_id=str(raw["release_id"]),
                artifact_root=Path(str(raw["artifact_root"])),
                artifact_manifest_sha256=str(raw["artifact_manifest_sha256"]),
                release_keyring_path=Path(str(raw["release_keyring_path"])),
                release_keyring_sha256=str(raw["release_keyring_sha256"]),
                target_machine_id_sha256=str(raw["target_machine_id_sha256"]),
                encryption_attestation_path=Path(
                    str(raw["encryption_attestation_path"])
                ),
                encryption_attestation_sha256=str(
                    raw["encryption_attestation_sha256"]
                ),
                python_binary=Path(
                    str(
                        raw.get(
                            "python_binary",
                            "/opt/ecorex/platform/python-3.11.9/bin/python3.11",
                        )
                    )
                ),
                postgres_binary=Path(
                    str(raw.get("postgres_binary", "/usr/bin/psql"))
                ),
                minio_binary=Path(
                    str(raw.get("minio_binary", "/opt/ecorex/platform/minio/minio"))
                ),
                nginx_binary=Path(str(raw.get("nginx_binary", "/usr/sbin/nginx"))),
                nginx_server_config=Path(
                    str(
                        raw.get(
                            "nginx_server_config",
                            "/etc/nginx/conf.d/ecorex-mvdcm.conf",
                        )
                    )
                ),
                systemctl_binary=Path(
                    str(raw.get("systemctl_binary", "/usr/bin/systemctl"))
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise CloudDeployError("deployment_spec_invalid") from None
        spec.validate()
        return spec

    def validate(self) -> None:
        if not SAFE_RELEASE_ID.fullmatch(self.release_id):
            raise CloudDeployError("release_id_invalid")
        for digest in (
            self.artifact_manifest_sha256,
            self.release_keyring_sha256,
            self.target_machine_id_sha256,
            self.encryption_attestation_sha256,
        ):
            if not SHA256.fullmatch(digest):
                raise CloudDeployError("deployment_digest_invalid")
        for path in (
            self.artifact_root,
            self.release_keyring_path,
            self.encryption_attestation_path,
            self.python_binary,
            self.postgres_binary,
            self.minio_binary,
            self.nginx_binary,
            self.nginx_server_config,
            self.systemctl_binary,
        ):
            pure = PurePosixPath(path.as_posix())
            if not pure.is_absolute() or ".." in pure.parts:
                raise CloudDeployError("deployment_path_outside_fence")
        if not _is_beneath(self.artifact_root, Path("/srv/ecorex-upload")):
            raise CloudDeployError("artifact_root_outside_fence")
        if not _is_beneath(self.release_keyring_path, CONFIG_ROOT):
            raise CloudDeployError("keyring_path_outside_fence")
        if not _is_beneath(self.encryption_attestation_path, CONFIG_ROOT):
            raise CloudDeployError("attestation_path_outside_fence")
        if self.python_binary != Path(
            "/opt/ecorex/platform/python-3.11.9/bin/python3.11"
        ):
            raise CloudDeployError("python_binary_outside_fence")
        if self.postgres_binary != Path("/usr/bin/psql"):
            raise CloudDeployError("postgres_binary_outside_fence")
        if self.minio_binary != Path("/opt/ecorex/platform/minio/minio"):
            raise CloudDeployError("minio_binary_outside_fence")
        if self.nginx_binary != Path("/usr/sbin/nginx"):
            raise CloudDeployError("nginx_binary_outside_fence")
        if self.nginx_server_config != NGINX_SERVER_CONFIG:
            raise CloudDeployError("nginx_server_config_outside_fence")
        if self.systemctl_binary != Path("/usr/bin/systemctl"):
            raise CloudDeployError("systemctl_binary_outside_fence")


@dataclasses.dataclass(frozen=True, slots=True)
class CloudDeploymentPlan:
    release_id: str
    current_slot: str | None
    target_slot: str
    actions: tuple[str, ...]
    blockers: tuple[str, ...]
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "release_id": self.release_id,
            "current_slot": self.current_slot,
            "target_slot": self.target_slot,
            "dry_run": self.dry_run,
            "actions": list(self.actions),
            "blockers": list(self.blockers),
        }


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        PurePosixPath(path.as_posix()).relative_to(PurePosixPath(root.as_posix()))
        return path != root
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise CloudDeployError("deployment_file_unreadable") from None
    return digest.hexdigest()


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CloudDeployError(code) from None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _validate_artifact(spec: CloudDeploymentSpec) -> Mapping[str, Any]:
    manifest_path = spec.artifact_root / "cloud-release-manifest.json"
    signature_path = spec.artifact_root / "cloud-release-manifest.sig.json"
    if _sha256_file(manifest_path) != spec.artifact_manifest_sha256:
        raise CloudDeployError("artifact_manifest_digest_mismatch")
    manifest = _read_json(manifest_path, "artifact_manifest_invalid")
    signature = _read_json(signature_path, "artifact_signature_invalid")
    keyring = _read_json(spec.release_keyring_path, "release_keyring_invalid")
    if _sha256_file(spec.release_keyring_path) != spec.release_keyring_sha256:
        raise CloudDeployError("release_keyring_digest_mismatch")
    if not isinstance(manifest, Mapping) or not isinstance(signature, Mapping):
        raise CloudDeployError("artifact_manifest_invalid")
    if set(manifest) != {
        "schema_version",
        "release_id",
        "version",
        "platform",
        "architecture",
        "python_version",
        "files",
    }:
        raise CloudDeployError("artifact_manifest_invalid")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("release_id") != spec.release_id
        or manifest.get("version") != PRODUCT_VERSION
        or manifest.get("platform") != "linux"
        or manifest.get("architecture") != TARGET_ARCHITECTURE
        or manifest.get("python_version") != PYTHON_VERSION
    ):
        raise CloudDeployError("artifact_target_mismatch")
    if signature.get("manifest_sha256") != spec.artifact_manifest_sha256:
        raise CloudDeployError("artifact_signature_invalid")
    key_id = signature.get("key_id")
    if not isinstance(key_id, str) or not isinstance(keyring, Mapping):
        raise CloudDeployError("artifact_signature_invalid")
    encoded_key = keyring.get(key_id)
    encoded_signature = signature.get("signature_b64")
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
        signed = base64.b64decode(encoded_signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signed, _canonical_json(manifest)
        )
    except Exception:
        raise CloudDeployError("artifact_signature_invalid") from None
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise CloudDeployError("artifact_manifest_invalid")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise CloudDeployError("artifact_manifest_invalid")
        relative = item.get("path")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if not isinstance(relative, str) or not SHA256.fullmatch(str(digest)):
            raise CloudDeployError("artifact_manifest_invalid")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative in seen:
            raise CloudDeployError("artifact_manifest_invalid")
        seen.add(relative)
        source = spec.artifact_root.joinpath(*pure.parts)
        if source.is_symlink() or not source.is_file():
            raise CloudDeployError("artifact_file_missing")
        try:
            actual_size = source.stat().st_size
        except OSError:
            raise CloudDeployError("artifact_file_unreadable") from None
        if actual_size != size or _sha256_file(source) != digest:
            raise CloudDeployError("artifact_file_digest_mismatch")
    required = {
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
    if not required.issubset(seen):
        raise CloudDeployError("artifact_entrypoint_missing")
    observed: set[str] = set()
    try:
        for current, directories, filenames in os.walk(
            spec.artifact_root, followlinks=False
        ):
            root = Path(current)
            for directory in directories:
                if (root / directory).is_symlink():
                    raise CloudDeployError("artifact_symlink_forbidden")
            for filename in filenames:
                path = root / filename
                if path.is_symlink():
                    raise CloudDeployError("artifact_symlink_forbidden")
                observed.add(path.relative_to(spec.artifact_root).as_posix())
    except OSError:
        raise CloudDeployError("artifact_file_unreadable") from None
    allowed = seen | {
        "cloud-release-manifest.json",
        "cloud-release-manifest.sig.json",
    }
    if observed != allowed:
        raise CloudDeployError("artifact_unlisted_file")
    return manifest


def _validate_attestation(spec: CloudDeploymentSpec) -> None:
    if _sha256_file(spec.encryption_attestation_path) != spec.encryption_attestation_sha256:
        raise CloudDeployError("encryption_attestation_digest_mismatch")
    value = _read_json(
        spec.encryption_attestation_path, "encryption_attestation_invalid"
    )
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "provider",
        "volume_id",
        "mount_root",
        "encrypted",
        "evidence_reference",
        "evidence_sha256",
    }:
        raise CloudDeployError("encryption_attestation_invalid")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("provider") not in {"luks2", "alibaba-cloud-kms"}
        or value.get("mount_root") != "/var/lib/ecorex"
        or value.get("encrypted") is not True
        or not isinstance(value.get("volume_id"), str)
        or not 3 <= len(value["volume_id"]) <= 128
        or not isinstance(value.get("evidence_reference"), str)
        or not 3 <= len(value["evidence_reference"]) <= 512
        or not SHA256.fullmatch(str(value.get("evidence_sha256")))
    ):
        raise CloudDeployError("encryption_attestation_invalid")


def _state() -> Mapping[str, Any] | None:
    path = STATE_ROOT / "active.json"
    if not path.exists():
        return None
    value = _read_json(path, "deployment_state_invalid")
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("active_slot") not in SLOTS
        or not SAFE_RELEASE_ID.fullmatch(str(value.get("active_release_id", "")))
    ):
        raise CloudDeployError("deployment_state_invalid")
    return value


def build_plan(spec: CloudDeploymentSpec, *, inspect_files: bool = True) -> CloudDeploymentPlan:
    blockers: list[str] = []
    if inspect_files:
        for check in (_validate_artifact, _validate_attestation):
            try:
                check(spec)
            except CloudDeployError as error:
                blockers.append(error.code)
    try:
        state = _state()
    except CloudDeployError as error:
        state = None
        blockers.append(error.code)
    current_slot = None if state is None else str(state["active_slot"])
    target_slot = "blue" if current_slot in {None, "green"} else "green"
    actions = (
        "verify_target_fence",
        "verify_signed_release",
        "verify_encrypted_persistent_volume_attestation",
        "verify_python_3_11_9_postgresql_15_nginx",
        "verify_encrypted_volume_secret_environment_files",
        "stage_immutable_release",
        "install_signed_systemd_and_nginx_templates",
        "verify_root_ecorex_cloud_environment_files",
        "run_control_plane_gateway_image_schema_migrations",
        "run_exact_control_plane_and_image_storage_contract_checks",
        "stop_previous_slot_after_drain",
        "start_candidate_slot_and_wait_for_readiness",
        "atomically_switch_nginx_upstream",
        "write_activation_receipt",
    )
    return CloudDeploymentPlan(
        release_id=spec.release_id,
        current_slot=current_slot,
        target_slot=target_slot,
        actions=actions,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _os_release() -> Mapping[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            name, raw = line.split("=", 1)
            values[name] = raw.strip().strip('"')
    except (OSError, UnicodeError):
        raise CloudDeployError("target_os_unavailable") from None
    return values


def _machine_id_sha256() -> str:
    try:
        value = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise CloudDeployError("target_machine_id_unavailable") from None
    if not re.fullmatch(r"[0-9a-fA-F]{16,64}", value):
        raise CloudDeployError("target_machine_id_invalid")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if callable(getter) else -1


def _run(
    command: Sequence[str],
    *,
    code: str,
    environment: Mapping[str, str] | None = None,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None if environment is None else dict(environment),
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise CloudDeployError(code) from None
    if result.returncode != 0:
        raise CloudDeployError(code)
    return result


def _target_preflight(spec: CloudDeploymentSpec, confirmation: str) -> None:
    if sys.platform != "linux":
        raise CloudDeployError("target_platform_mismatch")
    release = _os_release()
    if release.get("ID") != TARGET_OS_ID or release.get("VERSION_ID", "").split(".")[0] != TARGET_OS_VERSION:
        raise CloudDeployError("target_os_mismatch")
    if platform.machine().casefold() not in {"aarch64", "arm64"}:
        raise CloudDeployError("target_architecture_mismatch")
    actual = _machine_id_sha256()
    if confirmation != spec.target_machine_id_sha256 or actual != confirmation:
        raise CloudDeployError("target_machine_fence_mismatch")
    if _effective_user_id() != 0:
        raise CloudDeployError("target_root_required")
    result = _run(
        [str(spec.python_binary), "--version"], code="python_version_unavailable"
    )
    if result.stdout.decode("ascii", "ignore").strip() != f"Python {PYTHON_VERSION}":
        raise CloudDeployError("python_version_mismatch")
    postgres = _run(
        [str(spec.postgres_binary), "--version"], code="postgres_version_unavailable"
    )
    if not re.search(rb"\b15(?:\.\d+)?\b", postgres.stdout):
        raise CloudDeployError("postgres_version_mismatch")
    _run([str(spec.nginx_binary), "-v"], code="nginx_unavailable")
    _run([str(spec.systemctl_binary), "--version"], code="systemd_unavailable")
    _run(["/usr/sbin/runuser", "--version"], code="runuser_unavailable")
    _run(["/usr/bin/id", "-u", "ecorex-cloud"], code="service_identity_missing")
    _run(["/usr/bin/id", "-u", "ecorex-storage"], code="storage_identity_missing")
    _run(
        [str(spec.systemctl_binary), "is-active", "postgresql.service"],
        code="postgres_service_unavailable",
    )
    _validate_secret_environment_root()


def _ecorex_cloud_gid() -> int:
    if sys.platform != "linux":
        raise CloudDeployError("target_platform_mismatch")
    try:
        import grp

        return int(grp.getgrnam("ecorex-cloud").gr_gid)
    except (KeyError, OSError, ValueError):
        raise CloudDeployError("service_identity_missing") from None


def _validate_secret_environment_root() -> None:
    """Prove secret env files live on the attested encrypted volume.

    The attestation separately pins ``/var/lib/ecorex``.  This live check
    rejects a missing/unmounted volume and a permissive or substituted secret
    directory before any migration or service process receives credentials.
    """

    if sys.platform != "linux":
        raise CloudDeployError("target_platform_mismatch")
    try:
        mount = ENCRYPTED_VOLUME_ROOT.lstat()
        root = SECRET_ROOT.lstat()
    except OSError:
        raise CloudDeployError("secret_environment_root_unavailable") from None
    if (
        not os.path.ismount(ENCRYPTED_VOLUME_ROOT)
        or not stat.S_ISDIR(mount.st_mode)
        or not stat.S_ISDIR(root.st_mode)
        or ENCRYPTED_VOLUME_ROOT.is_symlink()
        or SECRET_ROOT.is_symlink()
        or mount.st_dev != root.st_dev
        or root.st_uid != 0
        or root.st_gid != _ecorex_cloud_gid()
        or stat.S_IMODE(root.st_mode) != 0o750
    ):
        raise CloudDeployError("secret_environment_root_invalid")


def _validate_secret_environment_file(path: Path) -> None:
    if not _is_beneath(path, SECRET_ROOT):
        raise CloudDeployError("secret_environment_path_outside_fence")
    try:
        metadata = path.lstat()
    except OSError:
        raise CloudDeployError("environment_file_unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o640
        or metadata.st_uid != 0
        or metadata.st_gid != _ecorex_cloud_gid()
    ):
        raise CloudDeployError("secret_environment_identity_invalid")


def _parse_env(path: Path, *, secret: bool) -> dict[str, str]:
    try:
        metadata = path.lstat()
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise CloudDeployError("environment_file_unavailable") from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CloudDeployError("environment_file_invalid")
    if secret and stat.S_IMODE(metadata.st_mode) != 0o640:
        raise CloudDeployError("secret_environment_permissions_invalid")
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise CloudDeployError("environment_file_invalid")
        name, value = stripped.split("=", 1)
        if not SAFE_ENV_NAME.fullmatch(name) or name in values or "\x00" in value:
            raise CloudDeployError("environment_file_invalid")
        single_quoted = value.startswith("'") or value.endswith("'")
        if value.startswith("'") or value.endswith("'"):
            if len(value) < 2 or not (value.startswith("'") and value.endswith("'")):
                raise CloudDeployError("environment_file_invalid")
            value = value[1:-1]
            if "'" in value:
                raise CloudDeployError("environment_file_invalid")
        if not single_quoted and '"' in value:
            # systemd removes unquoted double quotes while the migration
            # subprocess would not; rejecting avoids two configuration views.
            raise CloudDeployError("environment_file_invalid")
        values[name] = value
    if secret and not values:
        raise CloudDeployError("secret_environment_empty")
    return values


def _service_environment(service: str, slot: str) -> dict[str, str]:
    public_name, secret_name = ENV_NAMES[service]
    # Do not leak the invoking root shell's credentials into service/migration
    # processes. Every allowed value must be present in an owned env file.
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/var/lib/ecorex",
    }
    environment.update(_parse_env(CONFIG_ROOT / "config" / public_name, secret=False))
    secret_path = SECRET_ROOT / secret_name
    _validate_secret_environment_file(secret_path)
    environment.update(_parse_env(secret_path, secret=True))
    environment.update(
        _parse_env(CONFIG_ROOT / "slots" / slot / f"{service}.env", secret=False)
    )
    return environment


def _atomic_write(path: Path, payload: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _install_release(spec: CloudDeploymentSpec, manifest: Mapping[str, Any]) -> Path:
    destination = RELEASE_ROOT / spec.release_id
    if destination.exists():
        staged_spec = dataclasses.replace(spec, artifact_root=destination)
        _validate_artifact(staged_spec)
        return destination
    temporary = RELEASE_ROOT / f".{spec.release_id}.staging-{os.getpid()}"
    if temporary.exists():
        raise CloudDeployError("release_staging_collision")
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    try:
        shutil.copytree(spec.artifact_root, temporary, symlinks=False)
        staged_spec = dataclasses.replace(spec, artifact_root=temporary)
        _validate_artifact(staged_spec)
        os.replace(temporary, destination)
    except CloudDeployError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise CloudDeployError("release_staging_failed") from None
    return destination


def _legacy_admin_route_payload(source: bytes) -> tuple[bytes, bytes]:
    """Extract the exact v0.x Admin locations and replace them with one include."""

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise CloudDeployError("nginx_legacy_admin_route_invalid") from None
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    for header in LEGACY_ADMIN_LOCATION_HEADERS:
        matches = [
            index
            for index, line in enumerate(lines)
            if line.strip() == f"{header} {{"
        ]
        if len(matches) != 1:
            raise CloudDeployError("nginx_legacy_admin_route_invalid")
        start = matches[0]
        depth = 0
        end: int | None = None
        for index in range(start, len(lines)):
            statement = lines[index].split("#", 1)[0]
            depth += statement.count("{") - statement.count("}")
            if depth == 0:
                end = index + 1
                break
            if depth < 0:
                break
        if end is None or end <= start:
            raise CloudDeployError("nginx_legacy_admin_route_invalid")
        spans.append((start, end))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise CloudDeployError("nginx_legacy_admin_route_invalid")

    removed = {index for start, end in spans for index in range(start, end)}
    first = spans[0][0]
    indent = lines[first][: len(lines[first]) - len(lines[first].lstrip())]
    rendered: list[str] = []
    for index, line in enumerate(lines):
        if index == first:
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            rendered.append(f"{indent}{NGINX_ROUTE_INCLUDE}{newline}")
        if index not in removed:
            rendered.append(line)
    legacy = "".join(
        "".join(lines[start:end]).rstrip("\r\n") + "\n\n"
        for start, end in spans
    )
    migrated = "".join(rendered)
    if (
        migrated.count(NGINX_ROUTE_INCLUDE) != 1
        or any(f"{header} {{" in migrated for header in LEGACY_ADMIN_LOCATION_HEADERS)
        or not legacy.strip()
    ):
        raise CloudDeployError("nginx_legacy_admin_route_invalid")
    return migrated.encode("utf-8"), legacy.encode("utf-8")


def _restore_symlink(link: Path, previous: Path | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            link.unlink()
        return
    _atomic_symlink(previous, link)


def _validate_admin_route_resources() -> None:
    active = NGINX_ROOT / "active-admin-route.conf"
    legacy = NGINX_ROOT / "admin-route-legacy.conf"
    candidate = NGINX_ROOT / "admin-route-control-plane.conf"
    if NGINX_ROOT.is_symlink() or not NGINX_ROOT.is_dir() or not active.is_symlink():
        raise CloudDeployError("nginx_admin_route_wiring_invalid")
    payloads: dict[Path, bytes] = {}
    for path in (legacy, candidate):
        try:
            metadata = path.lstat()
            payload = path.read_bytes()
        except OSError:
            raise CloudDeployError("nginx_admin_route_wiring_invalid") from None
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022)
            or not 1 <= len(payload) <= 256 * 1024
        ):
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        payloads[path] = payload
    try:
        resolved = active.resolve(strict=True)
        allowed = {legacy.resolve(strict=True), candidate.resolve(strict=True)}
    except OSError:
        raise CloudDeployError("nginx_admin_route_wiring_invalid") from None
    if resolved not in allowed:
        raise CloudDeployError("nginx_admin_route_wiring_invalid")

    try:
        legacy_text = payloads[legacy].decode("utf-8", errors="strict")
        candidate_text = payloads[candidate].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise CloudDeployError("nginx_admin_route_wiring_invalid") from None
    if any(
        legacy_text.count(f"{header} {{") != 1
        for header in LEGACY_ADMIN_LOCATION_HEADERS
    ) or (
        candidate_text.count("location = /ecorex-agent/admin {") != 1
        or candidate_text.count("location ^~ /ecorex-agent/admin/ {") != 1
        or candidate_text.count("return 410;") != 2
        or candidate_text.count("proxy_pass $ecorex_control_plane;") != 1
        or "/srv/ecorex-agent-download" in candidate_text
    ):
        raise CloudDeployError("nginx_admin_route_wiring_invalid")


def _install_legacy_admin_route_wiring(spec: CloudDeploymentSpec) -> None:
    """Move live v0.x Admin locations behind a reversible second-level include."""

    server_config = spec.nginx_server_config
    try:
        metadata = server_config.lstat()
        source = server_config.read_bytes()
    except OSError:
        raise CloudDeployError("nginx_server_configuration_unavailable") from None
    if (
        server_config.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022)
        or not 1 <= len(source) <= 2 * 1024 * 1024
    ):
        raise CloudDeployError("nginx_server_configuration_invalid")

    include_count = source.count(NGINX_ROUTE_INCLUDE.encode("ascii"))
    active = NGINX_ROOT / "active-admin-route.conf"
    legacy = NGINX_ROOT / "admin-route-legacy.conf"
    if include_count == 1:
        if (
            any(
                f"{header} {{".encode("ascii") in source
                for header in LEGACY_ADMIN_LOCATION_HEADERS
            )
            or not active.is_symlink()
            or not legacy.is_file()
            or legacy.is_symlink()
        ):
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        _validate_admin_route_resources()
        return
    if include_count != 0:
        raise CloudDeployError("nginx_admin_route_wiring_invalid")

    migrated, legacy_payload = _legacy_admin_route_payload(source)
    backup = STATE_ROOT / "nginx-pre-v1.conf"
    if backup.exists():
        try:
            if backup.is_symlink() or backup.read_bytes() != source:
                raise OSError
        except OSError:
            raise CloudDeployError("nginx_legacy_backup_conflict") from None
    else:
        _atomic_write(backup, source, mode=0o600)
    _atomic_write(legacy, legacy_payload, mode=0o644)
    previous = active.resolve(strict=False) if active.exists() or active.is_symlink() else None
    _atomic_symlink(legacy, active)
    _validate_admin_route_resources()
    _atomic_write(
        server_config, migrated, mode=stat.S_IMODE(metadata.st_mode)
    )
    try:
        _run([str(spec.nginx_binary), "-t"], code="nginx_configuration_invalid")
        _run(
            [str(spec.systemctl_binary), "reload", "nginx.service"],
            code="nginx_reload_failed",
        )
    except CloudDeployError:
        _atomic_write(
            server_config, source, mode=stat.S_IMODE(metadata.st_mode)
        )
        _restore_symlink(active, previous)
        with contextlib.suppress(CloudDeployError):
            _run([str(spec.nginx_binary), "-t"], code="nginx_restore_failed")
            _run(
                [str(spec.systemctl_binary), "reload", "nginx.service"],
                code="nginx_restore_failed",
            )
        raise


def _install_deployment_templates(spec: CloudDeploymentSpec, release: Path) -> None:
    systemd_source = release / "deployment" / "systemd"
    nginx_source = release / "deployment" / "nginx"
    for name in (
        "ecorex-control-plane@.service",
        "ecorex-gateway@.service",
        "ecorex-image-api@.service",
        "ecorex-image-worker@.service",
    ):
        _atomic_write(SYSTEMD_ROOT / name, (systemd_source / name).read_bytes(), 0o644)
    for name in (
        "control-plane-blue.conf",
        "control-plane-green.conf",
        "control-plane-disabled.conf",
        "admin-route-control-plane.conf",
        "ecorex-cloud.routes.conf",
    ):
        _atomic_write(NGINX_ROOT / name, (nginx_source / name).read_bytes(), 0o644)
    active = NGINX_ROOT / "active-control-plane.conf"
    if not active.exists() and not active.is_symlink():
        _atomic_symlink(NGINX_ROOT / "control-plane-disabled.conf", active)
    _install_legacy_admin_route_wiring(spec)
    _systemctl(spec, "daemon-reload", ())


def _verify_nginx_wiring(spec: CloudDeploymentSpec) -> None:
    result = _run(
        [str(spec.nginx_binary), "-T"], code="nginx_configuration_invalid"
    )
    # The route fragment must be included by the existing TLS server. Merely
    # copying it into a directory is not accepted as production wiring.
    if (
        b"/etc/nginx/ecorex-cloud/active-control-plane.conf" not in result.stdout
        or b"/etc/nginx/ecorex-cloud/active-admin-route.conf" not in result.stdout
        or b"/ecorex-agent/admin/" not in result.stdout
    ):
        raise CloudDeployError("nginx_route_not_wired")


def _write_slot_environment(slot: str, release: Path) -> None:
    ports = PORTS[slot]
    values = {
        "control-plane": {
            "ECOREX_CP_BIND_HOST": "127.0.0.1",
            "ECOREX_CP_BIND_PORT": str(ports["control_plane"]),
            "ECOREX_CP_INSTANCE_ID": f"ecorex-cloud-{slot}",
        },
        "gateway": {
            "ECOREX_GATEWAY_BIND_HOST": "127.0.0.1",
            "ECOREX_GATEWAY_BIND_PORT": str(ports["gateway"]),
        },
        "image": {
            "ECOREX_IMAGE_BIND_HOST": "127.0.0.1",
            "ECOREX_IMAGE_BIND_PORT": str(ports["image"]),
            "ECOREX_IMAGE_INSTANCE_ID": f"ecorex-image-{slot}",
        },
    }
    for service, environment in values.items():
        payload = "".join(f"{name}={value}\n" for name, value in environment.items())
        _atomic_write(
            CONFIG_ROOT / "slots" / slot / f"{service}.env",
            payload.encode("ascii"),
        )
    _atomic_symlink(release, SLOT_ROOT / slot / "current")


_SERVICE_MODULES = {
    "control-plane": "ecorex.control_plane.production",
    "gateway": "ecorex.gateway.production",
    "image": "ecorex.image_orchestrator.production",
}


def _service_command(release: Path, service: str, *arguments: str) -> list[str]:
    module = _SERVICE_MODULES.get(service)
    if module is None:
        raise CloudDeployError("service_command_invalid")
    return [
        str(release / "venv" / "bin" / "python3.11"),
        "-m",
        module,
        *arguments,
    ]


def _verify_staged_runtime(release: Path) -> None:
    result = _run(
        [str(release / "venv" / "bin" / "python3.11"), "--version"],
        code="staged_python_version_unavailable",
    )
    if result.stdout.decode("ascii", "ignore").strip() != f"Python {PYTHON_VERSION}":
        raise CloudDeployError("staged_python_version_mismatch")


def _run_service_command(
    command: Sequence[str],
    *,
    code: str,
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "/usr/sbin/runuser",
            "--user",
            "ecorex-cloud",
            "--preserve-environment",
            "--",
            *command,
        ],
        code=code,
        environment=environment,
        timeout=timeout,
    )


def _schema_gate(release: Path, slot: str) -> None:
    for service in _SERVICE_MODULES:
        environment = _service_environment(service, slot)
        _run_service_command(
            _service_command(release, service, "schema", "migrate"),
            code=f"{service.replace('-', '_')}_schema_migration_failed",
            environment=environment,
            timeout=600,
        )
        # These checks intentionally execute the real S3 control/write/read/
        # delete probes.  MinIO is never accepted on API-compatibility faith.
        _run_service_command(
            _service_command(release, service, "schema", "check"),
            code=f"{service.replace('-', '_')}_production_contract_failed",
            environment=environment,
            timeout=600,
        )


def _unit(service: str, slot: str) -> str:
    return f"{service}@{slot}.service"


def _systemctl(spec: CloudDeploymentSpec, verb: str, units: Iterable[str]) -> None:
    command = [str(spec.systemctl_binary), verb, *units]
    _run(command, code=f"systemd_{verb.replace('-', '_')}_failed", timeout=180)


def _slot_units(slot: str) -> list[str]:
    return [_unit(service, slot) for service in SERVICE_NAMES]


def _wait_health(
    spec: CloudDeploymentSpec, slot: str, *, timeout_seconds: float = 120.0
) -> None:
    ports = PORTS[slot]
    endpoints = (
        f"http://127.0.0.1:{ports['control_plane']}/health/ready",
        f"http://127.0.0.1:{ports['gateway']}/health/ready",
        f"http://127.0.0.1:{ports['image']}/health/ready",
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready = True
        for endpoint in endpoints:
            try:
                request = urllib.request.Request(endpoint, method="GET")
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    ready = ready and response.status == 200
                    response.read(4096)
            except Exception:
                ready = False
        if ready:
            _run(
                [
                    str(spec.systemctl_binary),
                    "is-active",
                    _unit("ecorex-image-worker", slot),
                ],
                code="image_worker_unavailable",
            )
            return
        time.sleep(1.0)
    raise CloudDeployError("candidate_readiness_failed")


def _nginx_target(slot: str) -> Path:
    return NGINX_ROOT / f"control-plane-{slot}.conf"


def _switch_nginx(spec: CloudDeploymentSpec, slot: str) -> None:
    control_link = NGINX_ROOT / "active-control-plane.conf"
    admin_link = NGINX_ROOT / "active-admin-route.conf"
    control_previous = (
        control_link.resolve(strict=False)
        if control_link.exists() or control_link.is_symlink()
        else None
    )
    admin_previous = (
        admin_link.resolve(strict=False)
        if admin_link.exists() or admin_link.is_symlink()
        else None
    )
    try:
        _atomic_symlink(_nginx_target(slot), control_link)
        _atomic_symlink(NGINX_ROOT / "admin-route-control-plane.conf", admin_link)
        _validate_admin_route_resources()
        _run([str(spec.nginx_binary), "-t"], code="nginx_configuration_invalid")
        _run(
            [str(spec.systemctl_binary), "reload", "nginx.service"],
            code="nginx_reload_failed",
        )
    except (CloudDeployError, OSError) as error:
        with contextlib.suppress(OSError):
            _restore_symlink(control_link, control_previous)
        with contextlib.suppress(OSError):
            _restore_symlink(admin_link, admin_previous)
        with contextlib.suppress(CloudDeployError):
            _run([str(spec.nginx_binary), "-t"], code="nginx_restore_failed")
            _run(
                [str(spec.systemctl_binary), "reload", "nginx.service"],
                code="nginx_restore_failed",
            )
        if isinstance(error, CloudDeployError):
            raise
        raise CloudDeployError("nginx_route_switch_failed") from None


@contextlib.contextmanager
def _deployment_lock():
    if fcntl is None:
        raise CloudDeployError("target_platform_mismatch")
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CloudDeployError("deployment_lock_busy") from None
        yield
    finally:
        os.close(descriptor)


def _activation_state(
    *,
    release_id: str,
    slot: str,
    prior: Mapping[str, Any] | None,
    artifact_manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "active_release_id": release_id,
        "active_slot": slot,
        "previous_release_id": None if prior is None else prior["active_release_id"],
        "previous_slot": None if prior is None else prior["active_slot"],
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "activated_at_unix": int(time.time()),
    }


def deploy(spec: CloudDeploymentSpec, *, confirmation: str) -> Mapping[str, Any]:
    """Apply one local deployment after every immutable/target fence passes."""

    spec.validate()
    _target_preflight(spec, confirmation)
    manifest = _validate_artifact(spec)
    _validate_attestation(spec)
    with _deployment_lock():
        prior = _state()
        current = None if prior is None else str(prior["active_slot"])
        target = "blue" if current in {None, "green"} else "green"
        release = _install_release(spec, manifest)
        _install_deployment_templates(spec, release)
        _write_slot_environment(target, release)
        _verify_staged_runtime(release)
        _verify_nginx_wiring(spec)
        _schema_gate(release, target)
        if current is not None:
            _systemctl(spec, "stop", reversed(_slot_units(current)))
        candidate_units = _slot_units(target)
        try:
            _systemctl(spec, "start", candidate_units)
            _wait_health(spec, target)
            _switch_nginx(spec, target)
        except CloudDeployError:
            with contextlib.suppress(CloudDeployError):
                _systemctl(spec, "stop", reversed(candidate_units))
            if current is not None:
                with contextlib.suppress(CloudDeployError):
                    _systemctl(spec, "start", _slot_units(current))
                    _wait_health(spec, current)
            raise
        receipt = _activation_state(
            release_id=spec.release_id,
            slot=target,
            prior=prior,
            artifact_manifest_sha256=spec.artifact_manifest_sha256,
        )
        _atomic_write(
            STATE_ROOT / "active.json", _canonical_json(receipt) + b"\n", mode=0o600
        )
        _atomic_symlink(release, INSTALL_ROOT / "current")
        return receipt


def rollback(spec: CloudDeploymentSpec, *, confirmation: str) -> Mapping[str, Any]:
    """Return traffic to the recorded known-good slot without schema downgrade."""

    spec.validate()
    _target_preflight(spec, confirmation)
    _validate_attestation(spec)
    with _deployment_lock():
        current = _state()
        if current is None:
            raise CloudDeployError("rollback_state_missing")
        previous_slot = current.get("previous_slot")
        previous_release = current.get("previous_release_id")
        if previous_slot not in SLOTS or not SAFE_RELEASE_ID.fullmatch(
            str(previous_release or "")
        ):
            raise CloudDeployError("rollback_target_missing")
        previous_root = RELEASE_ROOT / str(previous_release)
        if not previous_root.is_dir():
            raise CloudDeployError("rollback_release_missing")
        active_slot = str(current["active_slot"])
        previous_units = _slot_units(str(previous_slot))
        _systemctl(spec, "stop", reversed(_slot_units(active_slot)))
        try:
            # Old code validates the already-migrated schema.  It is never
            # allowed to downgrade or restore a live database automatically.
            for service in _SERVICE_MODULES:
                _run_service_command(
                    _service_command(previous_root, service, "schema", "check"),
                    code="rollback_schema_incompatible",
                    environment=_service_environment(service, str(previous_slot)),
                    timeout=600,
                )
            _systemctl(spec, "start", previous_units)
            _wait_health(spec, str(previous_slot))
            _switch_nginx(spec, str(previous_slot))
        except CloudDeployError:
            with contextlib.suppress(CloudDeployError):
                _systemctl(spec, "stop", reversed(previous_units))
                _systemctl(spec, "start", _slot_units(active_slot))
                _wait_health(spec, active_slot)
            raise
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "active_release_id": previous_release,
            "active_slot": previous_slot,
            "previous_release_id": current["active_release_id"],
            "previous_slot": active_slot,
            "artifact_manifest_sha256": None,
            "activated_at_unix": int(time.time()),
        }
        _atomic_write(
            STATE_ROOT / "active.json", _canonical_json(receipt) + b"\n", mode=0o600
        )
        _atomic_symlink(previous_root, INSTALL_ROOT / "current")
        return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy-v1-cloud-sidecar")
    parser.add_argument("--spec", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm-target", default="")
    arguments = parser.parse_args(argv)
    try:
        spec = CloudDeploymentSpec.from_json(arguments.spec)
        if not arguments.apply and not arguments.rollback:
            print(json.dumps(build_plan(spec).to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        if not SHA256.fullmatch(arguments.confirm_target):
            raise CloudDeployError("target_confirmation_required")
        receipt = (
            rollback(spec, confirmation=arguments.confirm_target)
            if arguments.rollback
            else deploy(spec, confirmation=arguments.confirm_target)
        )
        # The receipt contains only public release/slot metadata.
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except CloudDeployError as error:
        print(json.dumps({"ok": False, "error": error.code}, sort_keys=True))
        return 2
    except Exception:
        # Never render an exception or child-process text; SDK errors and file
        # names can contain credentials or signed request material.
        print(json.dumps({"ok": False, "error": "deployment_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
