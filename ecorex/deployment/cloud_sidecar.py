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
from datetime import UTC, datetime
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from ecorex import __version__
from ecorex.deployment.cloud_artifact import (
    BUILD_CONTRACT,
    CLOUD_MANIFEST_SIGNING_DOMAIN,
)
from ecorex.deployment.provider_bridge_install import (
    ProviderBridgeInstallError,
    install_provider_bridge,
    validate_provider_bridge_materials,
)
from ecorex.migration.legacy_admin_management import (
    LegacyAdminManagementImportError,
    import_v0292_admin_management,
)
from ecorex.migration.legacy_identity_export import (
    LegacyIdentityExportError,
    export_v0292_legacy_identities,
)
from ecorex.migration.legacy_password_credentials import (
    LegacyPasswordCredentialImportError,
    import_v0292_password_credentials,
)
from ecorex.control_plane.management import (
    AdminManagementNotFound,
    AdminManagementRepository,
)
from ecorex.control_plane.management_models import CreateAdminUserRequest
from ecorex.control_plane.models import ControlPrincipal
from ecorex.release.public_index import (
    MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES,
    PublicBootstrapIndexError,
    unpublished_public_bootstrap_index,
    validate_public_bootstrap_index,
)
from ecorex.update import Ed25519SignatureVerifier, VerificationError

try:  # The read-only planner is intentionally importable on release workstations.
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI import coverage
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
PYTHON_VERSION = "3.11.9"
PRODUCT_VERSION = __version__
TARGET_OS_ID = "alinux"
TARGET_OS_VERSION = "4"
TARGET_ARCHITECTURE = "aarch64"
_PRODUCT_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)

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
ACTIVATION_JOURNAL_PATH = STATE_ROOT / "activation-pending.json"
LEGACY_ADMIN_DATABASE_PATH = Path(
    "/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3"
)
CONTROL_PLANE_DATABASE_PATH = Path(
    "/var/lib/ecorex/control-plane/control-plane.sqlite3"
)
RELEASE_REPLICA_ROOT = Path(
    "/srv/ecorex-agent-download/v1-artifacts"
)
RELEASE_REPLICA_PUBLIC_ROOT = "https://dl.ecoremedia.net/ecorex-agent/releases"
PUBLIC_BOOTSTRAP_ROOT = Path("/srv/ecorex-agent-download/public-pointer")
PUBLIC_BOOTSTRAP_INDEX_PATH = PUBLIC_BOOTSTRAP_ROOT / "public-bootstrap-index.json"
LEGACY_PUBLIC_BOOTSTRAP_INDEX_PATH = Path(
    "/srv/ecorex-agent-download/current/public-bootstrap-index.json"
)
PUBLIC_BOOTSTRAP_INDEX_URL = (
    "https://dl.ecoremedia.net/ecorex-agent/public-bootstrap-index.json"
)
PUBLICATION_KEYRING_PATH = CONFIG_ROOT / "publication-public-keys.json"
_DEPLOYMENT_PLATFORM_ADMIN_ACCOUNT_ID = "ecorex-platform-admin"
_DEPLOYMENT_PLATFORM_ADMIN_DISPLAY_NAME = "e-Mate 管理员"
_DEPLOYMENT_PLATFORM_ADMIN_ORGANIZATION_ID = "ecorex-production"
_DEPLOYMENT_PLATFORM_ADMIN_ACTOR = ControlPrincipal(
    subject="system.platform-admin-bootstrap",
    client_id="ecorex-production-bootstrap",
    account_id="system.deployment",
    organization_id=None,
    roles=frozenset({"platform_admin"}),
)

NGINX_ROUTE_INCLUDE = "include /etc/nginx/ecorex-cloud/ecorex-cloud.routes.conf;"
NGINX_LOGIN_HTTP_LIMITS = (
    "limit_req_zone $binary_remote_addr "
    "zone=ecorex_session_login_per_ip:10m rate=10r/m;\n"
    "limit_conn_zone $binary_remote_addr "
    "zone=ecorex_session_login_conn_per_ip:10m;\n\n"
)
LEGACY_ADMIN_LOCATION_HEADERS = (
    "location = /ecorex-agent/admin",
    "location ^~ /ecorex-agent/admin/api/",
    "location ^~ /ecorex-agent/api/admin/",
    "location ^~ /ecorex-agent/admin/",
)
LEGACY_POINTER_LOCATION_HEADER = (
    "location = /ecorex-agent/public-bootstrap-index.json"
)
LEGACY_DOWNLOAD_LOCATION_HEADER = "location ^~ /ecorex-agent/downloads/"
CONTROL_PLANE_ADMIN_ROUTE_CONTRACT = {
    "location = /ecorex-agent/admin": (
        "return 308 /ecorex-agent/admin/;",
    ),
    "location = /admin": (
        "return 308 /ecorex-agent/admin/;",
    ),
    "location ^~ /admin/": (
        "return 308 /ecorex-agent/admin/;",
    ),
    "location ^~ /ecorex-agent/admin/api/": ("return 410;",),
    "location ^~ /ecorex-agent/api/admin/": ("return 410;",),
    "location = /ecorex-agent/admin/health/ready": (
        "rewrite ^ /health/ready break;",
        "proxy_pass $ecorex_control_plane;",
        "proxy_http_version 1.1;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_buffering off;",
    ),
    "location ^~ /ecorex-agent/admin/": (
        "rewrite ^/ecorex-agent/admin/(.*)$ /admin/$1 break;",
        "proxy_pass $ecorex_control_plane;",
        "proxy_http_version 1.1;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_buffering off;",
    ),
}

SLOTS = ("blue", "green")
PORTS = {
    "blue": {
        "control_plane": 18771,
        "gateway": 18772,
        "image": 18773,
        "image_worker": 18774,
    },
    "green": {
        "control_plane": 18871,
        "gateway": 18872,
        "image": 18873,
        "image_worker": 18874,
    },
}
SERVICE_NAMES = (
    "ecorex-control-plane",
    "ecorex-gateway",
    "ecorex-image-api",
    "ecorex-image-worker",
)
API_SERVICE_NAMES = SERVICE_NAMES[:-1]
IMAGE_WORKER_SERVICE_NAME = SERVICE_NAMES[-1]
LEGACY_SERVICE_NAMES = (
    "ecorex-admin-api.service",
    "ecorex-usage-panel-api.service",
    "ecorex-web.service",
)
ENV_NAMES = {
    "control-plane": ("control-plane.env", "control-plane.secret.env"),
    "gateway": ("gateway.env", "gateway.secret.env"),
    "image": ("image.env", "image.secret.env"),
    "image-worker": ("image.env", "image.secret.env"),
}
SAFE_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}\Z")
SAFE_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_NAMESPACE = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
TARGET_TYPES = frozenset({"legacy", "slot"})
TRANSITION_OPERATIONS = frozenset({"activate", "rollback"})
TRANSITION_PHASES = frozenset(
    {
        "prepared",
        "migrating",
        "legacy_imported",
        "schema_ready",
        "target_ready",
        "routes_switched",
        "state_written",
    }
)


class CloudDeployError(RuntimeError):
    """Redacted deployment failure safe to show to an operator."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{3,80}", code):
            code = "deployment_failed"
        self.code = code
        super().__init__(code)


class _RecoverySourceSchemaIncompatible(CloudDeployError):
    """Internal direction signal; never start a source that cannot read the DB."""


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyAdminMigrationSpec:
    """Explicit first-activation authority for the fixed v0.2.9.2 source."""

    source_version: str

    @classmethod
    def from_value(cls, value: Any) -> "LegacyAdminMigrationSpec | None":
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"source_version"}:
            raise CloudDeployError("legacy_admin_migration_spec_invalid")
        spec = cls(source_version=str(value.get("source_version", "")))
        if spec.source_version != "0.2.9.2":
            raise CloudDeployError("legacy_admin_migration_spec_invalid")
        return spec


@dataclasses.dataclass(frozen=True, slots=True)
class _PublicBootstrapSeedIdentity:
    payload: bytes
    sha256: str
    size_bytes: int
    legacy_exact_route: bool


@dataclasses.dataclass(frozen=True, slots=True)
class CloudDeploymentSpec:
    release_id: str
    source_commit: str
    dependency_lock_manifest_sha256: str
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
    legacy_admin_migration: LegacyAdminMigrationSpec | None = None

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
            "source_commit",
            "dependency_lock_manifest_sha256",
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
            "legacy_admin_migration",
        }
        if set(raw) - expected:
            raise CloudDeployError("deployment_spec_unknown_field")
        try:
            spec = cls(
                release_id=str(raw["release_id"]),
                source_commit=str(raw["source_commit"]),
                dependency_lock_manifest_sha256=str(
                    raw["dependency_lock_manifest_sha256"]
                ),
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
                legacy_admin_migration=LegacyAdminMigrationSpec.from_value(
                    raw.get("legacy_admin_migration")
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise CloudDeployError("deployment_spec_invalid") from None
        spec.validate()
        return spec

    def validate(self) -> None:
        if not SAFE_RELEASE_ID.fullmatch(self.release_id):
            raise CloudDeployError("release_id_invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None:
            raise CloudDeployError("source_commit_invalid")
        for digest in (
            self.dependency_lock_manifest_sha256,
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
        if (
            self.legacy_admin_migration is not None
            and self.legacy_admin_migration.source_version != "0.2.9.2"
        ):
            raise CloudDeployError("legacy_admin_migration_spec_invalid")


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


def _utc_second(value: datetime | None = None) -> datetime:
    selected = value or datetime.now(UTC)
    if selected.tzinfo is None:
        raise CloudDeployError("legacy_admin_migration_contract_invalid")
    return selected.astimezone(UTC).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return _utc_second(value).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CloudDeployError("legacy_admin_migration_contract_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CloudDeployError("legacy_admin_migration_contract_invalid") from None
    normalized = _utc_second(parsed)
    if _utc_text(normalized) != value:
        raise CloudDeployError("legacy_admin_migration_contract_invalid")
    return normalized


def _legacy_migration_seed(
    spec: CloudDeploymentSpec, *, as_of: datetime | None = None
) -> dict[str, Any] | None:
    if spec.legacy_admin_migration is None:
        return None
    return {
        "source_version": "0.2.9.2",
        "as_of": _utc_text(_utc_second(as_of)),
        "source_database_sha256": None,
        "source_snapshot_sha256": None,
        "import_receipt_sha256": None,
        "identity_records_sha256": None,
    }


def _normalize_legacy_migration_contract(
    value: Any, *, phase: str
) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {
        "source_version",
        "as_of",
        "source_database_sha256",
        "source_snapshot_sha256",
        "import_receipt_sha256",
        "identity_records_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CloudDeployError("activation_journal_invalid")
    digests = (
        value.get("source_database_sha256"),
        value.get("source_snapshot_sha256"),
        value.get("import_receipt_sha256"),
        value.get("identity_records_sha256"),
    )
    if (
        value.get("source_version") != "0.2.9.2"
        or any(item is not None and not SHA256.fullmatch(str(item)) for item in digests)
    ):
        raise CloudDeployError("activation_journal_invalid")
    try:
        as_of = _utc_text(_parse_utc_text(value.get("as_of")))
    except CloudDeployError:
        raise CloudDeployError("activation_journal_invalid") from None
    complete = all(item is not None for item in digests)
    if phase in {
        "legacy_imported",
        "schema_ready",
        "target_ready",
        "routes_switched",
        "state_written",
    } and not complete:
        raise CloudDeployError("activation_journal_invalid")
    return {
        "source_version": "0.2.9.2",
        "as_of": as_of,
        "source_database_sha256": digests[0],
        "source_snapshot_sha256": digests[1],
        "import_receipt_sha256": digests[2],
        "identity_records_sha256": digests[3],
    }


def _product_version_key(value: object) -> tuple[int, int, int]:
    text = str(value)
    if _PRODUCT_SEMVER.fullmatch(text) is None:
        raise CloudDeployError("artifact_target_mismatch")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def _historical_product_version_is_compatible(value: object) -> bool:
    version = str(value)
    current = str(PRODUCT_VERSION)
    parsed = _product_version_key(version)
    return parsed <= _product_version_key(current) or (
        current == "0.3.0" and parsed[:2] == (1, 0) and parsed[2] <= 17
    )


def _validate_artifact(
    spec: CloudDeploymentSpec,
    *,
    historical_release: bool = False,
) -> Mapping[str, Any]:
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
        "build_contract",
        "source_commit",
        "dependency_lock_manifest_sha256",
        "files",
    }:
        raise CloudDeployError("artifact_manifest_invalid")
    version = manifest.get("version")
    version_matches = (
        _historical_product_version_is_compatible(version)
        if historical_release
        else version == PRODUCT_VERSION
    )
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("release_id") != spec.release_id
        or not version_matches
        or manifest.get("platform") != "linux"
        or manifest.get("architecture") != TARGET_ARCHITECTURE
        or manifest.get("python_version") != PYTHON_VERSION
        or manifest.get("build_contract") != BUILD_CONTRACT
        or manifest.get("source_commit") != spec.source_commit
        or manifest.get("dependency_lock_manifest_sha256")
        != spec.dependency_lock_manifest_sha256
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
            signed,
            CLOUD_MANIFEST_SIGNING_DOMAIN + _canonical_json(manifest),
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
            "posix_mode",
        }:
            raise CloudDeployError("artifact_manifest_invalid")
        relative = item.get("path")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        posix_mode = item.get("posix_mode")
        if not isinstance(relative, str) or not SHA256.fullmatch(str(digest)):
            raise CloudDeployError("artifact_manifest_invalid")
        if posix_mode not in {"0644", "0755"}:
            raise CloudDeployError("artifact_manifest_invalid")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative in seen:
            raise CloudDeployError("artifact_manifest_invalid")
        seen.add(relative)
        source = spec.artifact_root.joinpath(*pure.parts)
        if source.is_symlink() or not source.is_file():
            raise CloudDeployError("artifact_file_missing")
        try:
            metadata = source.stat()
            actual_size = metadata.st_size
        except OSError:
            raise CloudDeployError("artifact_file_unreadable") from None
        if actual_size != size or _sha256_file(source) != digest:
            raise CloudDeployError("artifact_file_digest_mismatch")
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if os.name == "nt":
            actual_mode = 0o755 if relative.startswith("venv/bin/") else 0o644
        if f"{actual_mode:04o}" != posix_mode:
            raise CloudDeployError("artifact_file_mode_mismatch")
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


def _normalized_slot_state(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CloudDeployError(code)
    active_release_id = value.get("active_release_id")
    active_slot = value.get("active_slot")
    previous_release_id = value.get("previous_release_id")
    previous_slot = value.get("previous_slot")
    previous_target_type = value.get("previous_target_type")
    if previous_target_type is None:
        previous_target_type = (
            "slot"
            if previous_slot in SLOTS
            and SAFE_RELEASE_ID.fullmatch(str(previous_release_id or ""))
            else "legacy"
        )
    artifact_digest = value.get("artifact_manifest_sha256")
    activated_at = value.get("activated_at_unix")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("active_target_type", "slot") != "slot"
        or active_slot not in SLOTS
        or not SAFE_RELEASE_ID.fullmatch(str(active_release_id or ""))
        or previous_target_type not in TARGET_TYPES
        or (
            previous_target_type == "slot"
            and (
                previous_slot not in SLOTS
                or not SAFE_RELEASE_ID.fullmatch(str(previous_release_id or ""))
            )
        )
        or (
            previous_target_type == "legacy"
            and (previous_slot is not None or previous_release_id is not None)
        )
        or (artifact_digest is not None and not SHA256.fullmatch(str(artifact_digest)))
        or isinstance(activated_at, bool)
        or not isinstance(activated_at, int)
        or activated_at < 1
    ):
        raise CloudDeployError(code)
    return {
        "schema_version": SCHEMA_VERSION,
        "active_target_type": "slot",
        "active_release_id": str(active_release_id),
        "active_slot": str(active_slot),
        "previous_target_type": str(previous_target_type),
        "previous_release_id": previous_release_id,
        "previous_slot": previous_slot,
        "artifact_manifest_sha256": artifact_digest,
        "activated_at_unix": activated_at,
    }


def _state() -> Mapping[str, Any] | None:
    path = STATE_ROOT / "active.json"
    if not path.exists():
        return None
    value = _read_json(path, "deployment_state_invalid")
    return _normalized_slot_state(value, "deployment_state_invalid")


def _validate_legacy_migration_plan(
    spec: CloudDeploymentSpec, state: Mapping[str, Any] | None
) -> None:
    if state is not None:
        return
    source_exists = LEGACY_ADMIN_DATABASE_PATH.exists()
    if source_exists and spec.legacy_admin_migration is None:
        raise CloudDeployError("legacy_admin_migration_required")
    if spec.legacy_admin_migration is None:
        return
    if not source_exists:
        raise CloudDeployError("legacy_admin_database_unavailable")
    cutoff = _utc_second()
    try:
        import_v0292_admin_management(
            LEGACY_ADMIN_DATABASE_PATH,
            CONTROL_PLANE_DATABASE_PATH,
            encryption_key=None,
            dry_run=True,
            as_of=cutoff,
        )
        export_v0292_legacy_identities(
            LEGACY_ADMIN_DATABASE_PATH,
            as_of=cutoff,
        )
    except (LegacyAdminManagementImportError, LegacyIdentityExportError):
        raise CloudDeployError("legacy_admin_migration_preflight_failed") from None


def build_plan(spec: CloudDeploymentSpec, *, inspect_files: bool = True) -> CloudDeploymentPlan:
    blockers: list[str] = []
    try:
        _target_preflight(spec, spec.target_machine_id_sha256)
    except CloudDeployError as error:
        blockers.append(error.code)
    if inspect_files:
        for check in (_validate_artifact, _validate_attestation):
            try:
                check(spec)
            except CloudDeployError as error:
                blockers.append(error.code)
        try:
            validate_provider_bridge_materials()
        except ProviderBridgeInstallError as error:
            blockers.append(error.code)
    try:
        state = _state()
    except CloudDeployError as error:
        state = None
        blockers.append(error.code)
    try:
        _validate_legacy_migration_plan(spec, state)
    except CloudDeployError as error:
        blockers.append(error.code)
    try:
        pending_transition = _transition_journal()
    except CloudDeployError as error:
        pending_transition = None
        blockers.append(error.code)
    if pending_transition is not None:
        blockers.append("activation_recovery_required")
    current_slot = None if state is None else str(state["active_slot"])
    target_slot = "blue" if current_slot in {None, "green"} else "green"
    actions = (
        "verify_target_fence",
        "verify_signed_release",
        "verify_encrypted_persistent_volume_attestation",
        "verify_python_3_11_9_postgresql_15_nginx",
        "verify_encrypted_volume_secret_environment_files",
        "validate_and_install_loopback_provider_tls_bridge",
        "recover_incomplete_activation_before_new_mutation",
        "stage_immutable_release",
        "prepare_exact_release_replica_storage_permissions",
        "validate_and_seed_legacy_public_bootstrap_before_exact_route_switch",
        "install_signed_systemd_and_nginx_templates",
        "verify_root_ecorex_cloud_environment_files",
        "run_control_plane_gateway_image_schema_migrations",
        "freeze_and_import_v0292_admin_and_identity_in_activation_journal",
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
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            input=input_bytes,
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
    _validate_base_environment_files()


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


def _validate_base_environment_files() -> None:
    """Read-only equivalent of the env dependency checks used at activation."""

    for public_name, secret_name in ENV_NAMES.values():
        _parse_env(CONFIG_ROOT / "config" / public_name, secret=False)
        secret_path = SECRET_ROOT / secret_name
        _validate_secret_environment_file(secret_path)
        _parse_env(secret_path, secret=True)


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


def _fsync_directory(path: Path) -> None:
    """Durably sync one real directory without following a substituted link."""

    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(before.st_mode)
            or path.resolve(strict=True) != path
        ):
            raise OSError
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise OSError
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise CloudDeployError("deployment_directory_sync_failed") from None


def _transition_journal() -> dict[str, Any] | None:
    path = ACTIVATION_JOURNAL_PATH
    if not path.exists() and not path.is_symlink():
        return None
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError:
        raise CloudDeployError("activation_journal_invalid") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
        or not 1 <= len(payload) <= 64 * 1024
    ):
        raise CloudDeployError("activation_journal_invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise CloudDeployError("activation_journal_invalid") from None
    required = {
        "schema_version",
        "operation",
        "phase",
        "source_target_type",
        "source_state",
        "target_target_type",
        "target_state",
        "created_at_unix",
        "legacy_admin_migration",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise CloudDeployError("activation_journal_invalid")
    source_type = value.get("source_target_type")
    target_type = value.get("target_target_type")
    operation = value.get("operation")
    phase = value.get("phase")
    created_at = value.get("created_at_unix")
    if (
        source_type not in TARGET_TYPES
        or target_type not in TARGET_TYPES
        or operation not in TRANSITION_OPERATIONS
        or phase not in TRANSITION_PHASES
        or isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at < 1
    ):
        raise CloudDeployError("activation_journal_invalid")
    source_state = value.get("source_state")
    target_state = value.get("target_state")
    if source_type == "slot":
        source_state = _normalized_slot_state(source_state, "activation_journal_invalid")
    elif source_state is not None:
        raise CloudDeployError("activation_journal_invalid")
    if target_type == "slot":
        target_state = _normalized_slot_state(target_state, "activation_journal_invalid")
    elif target_state is not None:
        raise CloudDeployError("activation_journal_invalid")
    if (
        source_type == "slot"
        and target_type == "slot"
        and source_state["active_slot"] == target_state["active_slot"]
    ):
        raise CloudDeployError("activation_journal_invalid")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "operation": str(operation),
        "phase": str(phase),
        "source_target_type": str(source_type),
        "source_state": source_state,
        "target_target_type": str(target_type),
        "target_state": target_state,
        "created_at_unix": created_at,
        "legacy_admin_migration": _normalize_legacy_migration_contract(
            value.get("legacy_admin_migration"), phase=str(phase)
        ),
    }
    if payload != _canonical_json(normalized) + b"\n":
        raise CloudDeployError("activation_journal_invalid")
    return normalized


def _write_transition_journal(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    _atomic_write(
        ACTIVATION_JOURNAL_PATH,
        _canonical_json(normalized) + b"\n",
        mode=0o600,
    )
    _fsync_directory(STATE_ROOT)
    parsed = _transition_journal()
    if parsed is None:
        raise CloudDeployError("activation_journal_invalid")
    return parsed


def _advance_transition_journal(
    journal: Mapping[str, Any], phase: str
) -> dict[str, Any]:
    if phase not in TRANSITION_PHASES:
        raise CloudDeployError("activation_journal_invalid")
    updated = dict(journal)
    updated["phase"] = phase
    return _write_transition_journal(updated)


def _clear_transition_journal() -> None:
    if not ACTIVATION_JOURNAL_PATH.exists() and not ACTIVATION_JOURNAL_PATH.is_symlink():
        # The unlink is the commit point. If a prior clear removed the entry
        # but directory fsync raised, target completion may safely finish and
        # treat the already-absent journal as committed.
        _fsync_directory(STATE_ROOT)
        return
    try:
        metadata = ACTIVATION_JOURNAL_PATH.lstat()
        if ACTIVATION_JOURNAL_PATH.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        ACTIVATION_JOURNAL_PATH.unlink()
    except OSError:
        raise CloudDeployError("activation_journal_clear_failed") from None
    _fsync_directory(STATE_ROOT)


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, link)


_RELEASE_DIRECTORY_MODE = 0o555
_RECOVERABLE_RELEASE_DIRECTORY_MODES = frozenset(
    {_RELEASE_DIRECTORY_MODE, 0o700, 0o755}
)


def _release_directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise CloudDeployError("release_directory_invalid") from None
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
        or metadata.st_uid != 0
        or metadata.st_gid != 0
    ):
        raise CloudDeployError("release_directory_identity_invalid")
    if (
        stat.S_IMODE(metadata.st_mode)
        not in _RECOVERABLE_RELEASE_DIRECTORY_MODES
    ):
        raise CloudDeployError("release_directory_mode_invalid")
    return metadata.st_dev, metadata.st_ino


def _seal_release_directory(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode)
            not in _RECOVERABLE_RELEASE_DIRECTORY_MODES
        ):
            raise OSError
        if stat.S_IMODE(metadata.st_mode) != _RELEASE_DIRECTORY_MODE:
            path.chmod(_RELEASE_DIRECTORY_MODE)
        sealed = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(sealed.st_mode)
            or (sealed.st_dev, sealed.st_ino) != identity
            or sealed.st_uid != 0
            or sealed.st_gid != 0
            or stat.S_IMODE(sealed.st_mode) != _RELEASE_DIRECTORY_MODE
        ):
            raise OSError
    except OSError:
        raise CloudDeployError("release_directory_seal_failed") from None


def _install_release(spec: CloudDeploymentSpec, manifest: Mapping[str, Any]) -> Path:
    del manifest
    destination = RELEASE_ROOT / spec.release_id
    if destination.exists() or destination.is_symlink():
        identity = _release_directory_identity(destination)
        staged_spec = dataclasses.replace(spec, artifact_root=destination)
        _validate_artifact(staged_spec)
        _seal_release_directory(destination, identity)
        return destination
    temporary = RELEASE_ROOT / f".{spec.release_id}.staging-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise CloudDeployError("release_staging_collision")
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    try:
        shutil.copytree(spec.artifact_root, temporary, symlinks=False)
        identity = _release_directory_identity(temporary)
        staged_spec = dataclasses.replace(spec, artifact_root=temporary)
        _validate_artifact(staged_spec)
        _seal_release_directory(temporary, identity)
        # The sealed, fully verified inode is published by one atomic rename.
        # Do not introduce a fallible post-publication step that could leave a
        # new release visible without a completed install result.
        os.replace(temporary, destination)
    except CloudDeployError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise CloudDeployError("release_staging_failed") from None
    return destination


def _legacy_admin_route_payload(source: bytes) -> tuple[bytes, bytes]:
    """Extract v0.x Admin and retire its mutable-pointer slot alias.

    The Admin locations remain available behind the reversible legacy include.
    An existing exact Bootstrap-pointer location is deliberately not copied to
    that include: the v1 cloud route owns the independent mutable object path.
    A legacy catch-all ``/ecorex-agent/`` route may remain and is safely
    shadowed by the new exact location.
    """

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise CloudDeployError("nginx_legacy_admin_route_invalid") from None
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    admin_spans: list[tuple[int, int]] = []
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
        admin_spans.append((start, end))
    pointer_matches = [
        index
        for index, line in enumerate(lines)
        if line.strip() == f"{LEGACY_POINTER_LOCATION_HEADER} {{"
    ]
    if len(pointer_matches) > 1:
        raise CloudDeployError("nginx_legacy_admin_route_invalid")
    if pointer_matches:
        start = pointer_matches[0]
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
        for start, end in admin_spans
    )
    migrated = "".join(rendered)
    if (
        migrated.count(NGINX_ROUTE_INCLUDE) != 1
        or any(f"{header} {{" in migrated for header in LEGACY_ADMIN_LOCATION_HEADERS)
        or f"{LEGACY_POINTER_LOCATION_HEADER} {{" in migrated
        or not legacy.strip()
    ):
        raise CloudDeployError("nginx_legacy_admin_route_invalid")
    return migrated.encode("utf-8"), legacy.encode("utf-8")


def _without_legacy_managed_locations(source: bytes) -> bytes:
    """Remove legacy locations now owned by the signed managed include."""

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise CloudDeployError("nginx_legacy_admin_route_invalid") from None
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    headers = (LEGACY_POINTER_LOCATION_HEADER, LEGACY_DOWNLOAD_LOCATION_HEADER)
    for header in headers:
        matches = [
            index for index, line in enumerate(lines) if line.strip() == f"{header} {{"
        ]
        if len(matches) > 1:
            raise CloudDeployError("nginx_legacy_admin_route_invalid")
        if not matches:
            continue
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
    if not spans:
        return source
    removed = {index for start, end in spans for index in range(start, end)}
    migrated = "".join(line for index, line in enumerate(lines) if index not in removed)
    if any(f"{header} {{" in migrated for header in headers):
        raise CloudDeployError("nginx_legacy_admin_route_invalid")
    return migrated.encode("utf-8")


def _restore_symlink(link: Path, previous: Path | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            link.unlink()
        return
    _atomic_symlink(previous, link)


def _nginx_location_contract(text: str) -> dict[str, tuple[str, ...]]:
    """Parse a locations-only fragment into an exact, comment-free contract."""

    lines = text.splitlines()
    contract: dict[str, tuple[str, ...]] = {}
    index = 0
    while index < len(lines):
        statement = lines[index].split("#", 1)[0].strip()
        index += 1
        if not statement:
            continue
        if not statement.startswith("location ") or not statement.endswith("{"):
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        header = statement[:-1].rstrip()
        if header in contract:
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        directives: list[str] = []
        closed = False
        while index < len(lines):
            directive = lines[index].split("#", 1)[0].strip()
            index += 1
            if not directive:
                continue
            if directive == "}":
                closed = True
                break
            # The reviewed Admin fragment intentionally has no nested blocks.
            # Reject them rather than attempting a permissive Nginx parse.
            if "{" in directive or "}" in directive:
                raise CloudDeployError("nginx_admin_route_wiring_invalid")
            directives.append(directive)
        if not closed:
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        contract[header] = tuple(directives)
    return contract


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
    ) or "/srv/ecorex-agent-download" in candidate_text:
        raise CloudDeployError("nginx_admin_route_wiring_invalid")
    try:
        candidate_contract = _nginx_location_contract(candidate_text)
    except CloudDeployError:
        raise CloudDeployError("nginx_admin_route_wiring_invalid") from None
    if candidate_contract != CONTROL_PLANE_ADMIN_ROUTE_CONTRACT:
        raise CloudDeployError("nginx_admin_route_wiring_invalid")


def _install_legacy_admin_route_wiring(
    spec: CloudDeploymentSpec,
    *,
    public_bootstrap_seed: _PublicBootstrapSeedIdentity,
) -> None:
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

    guarded_source = _with_login_http_limits(source)
    include_count = guarded_source.count(NGINX_ROUTE_INCLUDE.encode("ascii"))
    active = NGINX_ROOT / "active-admin-route.conf"
    legacy = NGINX_ROOT / "admin-route-legacy.conf"
    if include_count == 1:
        if (
            any(
                f"{header} {{".encode("ascii") in guarded_source
                for header in LEGACY_ADMIN_LOCATION_HEADERS
            )
            or not active.is_symlink()
            or not legacy.is_file()
            or legacy.is_symlink()
        ):
            raise CloudDeployError("nginx_admin_route_wiring_invalid")
        _validate_admin_route_resources()
        migrated = _without_legacy_managed_locations(guarded_source)
        if migrated != source:
            _verify_public_bootstrap_seed_before_route_retire(
                spec, public_bootstrap_seed
            )
            _atomic_write(
                server_config,
                migrated,
                mode=stat.S_IMODE(metadata.st_mode),
            )
            try:
                _run(
                    [str(spec.nginx_binary), "-t"],
                    code="nginx_configuration_invalid",
                )
                _run(
                    [str(spec.systemctl_binary), "reload", "nginx.service"],
                    code="nginx_reload_failed",
                )
            except CloudDeployError:
                _atomic_write(
                    server_config,
                    source,
                    mode=stat.S_IMODE(metadata.st_mode),
                )
                with contextlib.suppress(CloudDeployError):
                    _run(
                        [str(spec.nginx_binary), "-t"],
                        code="nginx_restore_failed",
                    )
                    _run(
                        [str(spec.systemctl_binary), "reload", "nginx.service"],
                        code="nginx_restore_failed",
                    )
                raise
        else:
            _verify_public_bootstrap_seed_before_route_retire(
                spec, public_bootstrap_seed
            )
        return
    if include_count != 0:
        raise CloudDeployError("nginx_admin_route_wiring_invalid")

    migrated, legacy_payload = _legacy_admin_route_payload(guarded_source)
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
    _verify_public_bootstrap_seed_before_route_retire(
        spec, public_bootstrap_seed
    )
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


def _with_login_http_limits(source: bytes) -> bytes:
    """Install login limit zones at the conf.d/http level, never in a server include."""

    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise CloudDeployError("nginx_server_configuration_invalid") from None
    request_zone = "zone=ecorex_session_login_per_ip:10m"
    connection_zone = "zone=ecorex_session_login_conn_per_ip:10m"
    expected = NGINX_LOGIN_HTTP_LIMITS
    if text.startswith(expected):
        if text.count(request_zone) != 1 or text.count(connection_zone) != 1:
            raise CloudDeployError("nginx_login_limit_wiring_invalid")
        return source
    if request_zone in text or connection_zone in text:
        raise CloudDeployError("nginx_login_limit_wiring_invalid")
    return (expected + text).encode("utf-8")


def _install_deployment_templates(spec: CloudDeploymentSpec, release: Path) -> None:
    _prepare_release_replica_storage()
    _prepare_public_bootstrap_storage()
    _install_publication_keyring(spec)
    public_bootstrap_seed = _seed_legacy_public_bootstrap_pointer(spec)
    try:
        provider_bridge = validate_provider_bridge_materials()
        install_provider_bridge(provider_bridge)
    except ProviderBridgeInstallError as error:
        raise CloudDeployError(error.code) from None
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
    _install_legacy_admin_route_wiring(
        spec, public_bootstrap_seed=public_bootstrap_seed
    )
    _systemctl(spec, "daemon-reload", ())


def _prepare_release_replica_storage() -> None:
    """Provision only the already-authorized CDN subtree for ecorex-cloud."""

    try:
        environment = _parse_env(
            CONFIG_ROOT / "config" / "control-plane.env", secret=False
        )
    except CloudDeployError:
        raise CloudDeployError("release_replica_configuration_invalid") from None
    base_namespace = environment.get("ECOREX_CP_RELEASE_REPLICA_NAMESPACE", "")
    base_product_version = environment.get(
        "ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION", ""
    )
    namespace = f"v{PRODUCT_VERSION}"
    try:
        base_version_is_compatible = _historical_product_version_is_compatible(
            base_product_version
        )
    except CloudDeployError:
        base_version_is_compatible = False
    if (
        environment.get("ECOREX_CP_RELEASE_REPLICA_ENABLED") != "true"
        or environment.get("ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT")
        != str(RELEASE_REPLICA_ROOT)
        or environment.get("ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT")
        != RELEASE_REPLICA_PUBLIC_ROOT
        or not base_version_is_compatible
        or base_namespace != f"v{base_product_version}"
        or RELEASE_NAMESPACE.fullmatch(base_namespace) is None
        or RELEASE_NAMESPACE.fullmatch(namespace) is None
    ):
        raise CloudDeployError("release_replica_configuration_invalid")
    namespace_root = RELEASE_REPLICA_ROOT / namespace
    try:
        if (
            RELEASE_REPLICA_ROOT.is_symlink()
            or RELEASE_REPLICA_ROOT.resolve(strict=True) != RELEASE_REPLICA_ROOT
        ):
            raise OSError
        namespace_root.mkdir(mode=0o755, exist_ok=True)
        for directory in (
            namespace_root,
            namespace_root / "stable",
            namespace_root / "canary",
        ):
            directory.mkdir(mode=0o755, exist_ok=True)
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or directory.resolve(strict=True) != directory
            ):
                raise OSError
            shutil.chown(directory, user="ecorex-cloud", group="ecorex-storage")
            os.chmod(directory, 0o755)
        _fsync_directory(namespace_root)
        _fsync_directory(RELEASE_REPLICA_ROOT)
    except OSError:
        raise CloudDeployError("release_replica_storage_invalid") from None


def _prepare_public_bootstrap_storage() -> None:
    """Provision the CP-owned, publicly readable mutable pointer directory."""

    try:
        environment = _parse_env(
            CONFIG_ROOT / "config" / "control-plane.env", secret=False
        )
    except CloudDeployError:
        raise CloudDeployError("public_bootstrap_storage_configuration_invalid") from None
    if (
        environment.get("ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_PATH")
        != str(PUBLIC_BOOTSTRAP_INDEX_PATH)
        or environment.get("ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_URL")
        != PUBLIC_BOOTSTRAP_INDEX_URL
    ):
        raise CloudDeployError("public_bootstrap_storage_configuration_invalid")
    download_root = PUBLIC_BOOTSTRAP_ROOT.parent
    try:
        download_metadata = download_root.lstat()
        if (
            stat.S_ISLNK(download_metadata.st_mode)
            or not stat.S_ISDIR(download_metadata.st_mode)
            or download_root.resolve(strict=True) != download_root
        ):
            raise OSError
        PUBLIC_BOOTSTRAP_ROOT.mkdir(mode=0o755, exist_ok=True)
        metadata = PUBLIC_BOOTSTRAP_ROOT.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or PUBLIC_BOOTSTRAP_ROOT.resolve(strict=True) != PUBLIC_BOOTSTRAP_ROOT
            or metadata.st_dev != download_metadata.st_dev
        ):
            raise OSError
        shutil.chown(
            PUBLIC_BOOTSTRAP_ROOT,
            user="ecorex-cloud",
            group="ecorex-storage",
        )
        os.chmod(PUBLIC_BOOTSTRAP_ROOT, 0o755)
        if os.path.lexists(PUBLIC_BOOTSTRAP_INDEX_PATH):
            pointer = PUBLIC_BOOTSTRAP_INDEX_PATH.lstat()
            if (
                stat.S_ISLNK(pointer.st_mode)
                or not stat.S_ISREG(pointer.st_mode)
                or pointer.st_dev != download_metadata.st_dev
                or getattr(pointer, "st_nlink", 1) != 1
            ):
                raise OSError
            shutil.chown(
                PUBLIC_BOOTSTRAP_INDEX_PATH,
                user="ecorex-cloud",
                group="ecorex-storage",
            )
            os.chmod(PUBLIC_BOOTSTRAP_INDEX_PATH, 0o644)
        _fsync_directory(PUBLIC_BOOTSTRAP_ROOT)
        _fsync_directory(download_root)
    except (LookupError, OSError):
        raise CloudDeployError("public_bootstrap_storage_invalid") from None


def _read_public_bootstrap_file(path: Path, *, code: str) -> bytes:
    download_root = PUBLIC_BOOTSTRAP_ROOT.parent
    try:
        root = download_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or getattr(before, "st_nlink", 1) != 1
            or not 1 <= before.st_size <= MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
        ):
            raise OSError
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if any(
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
            )
            != identity
            for item in (opened, after, current)
        ) or len(payload) != before.st_size:
            raise OSError
        return payload
    except (OSError, ValueError):
        raise CloudDeployError(code) from None


def _validate_public_bootstrap_seed_payload(
    spec: CloudDeploymentSpec, payload: bytes
) -> None:
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise PublicBootstrapIndexError("public index must be an object")
        if value.get("status") == "unpublished":
            validate_public_bootstrap_index(value, allow_expired_freshness=True)
            return
        release_raw, publication_raw = _public_pointer_keyrings(spec)
        validate_public_bootstrap_index(
            value,
            verifier=Ed25519SignatureVerifier(release_raw),
            freshness_verifier=Ed25519SignatureVerifier(publication_raw),
            allow_expired_freshness=True,
            allow_legacy_v1017_sequence=True,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        PublicBootstrapIndexError,
        VerificationError,
        TypeError,
        ValueError,
    ):
        raise CloudDeployError("legacy_public_bootstrap_seed_invalid") from None


def _legacy_pointer_route_present(spec: CloudDeploymentSpec) -> bool:
    try:
        metadata = spec.nginx_server_config.lstat()
        payload = spec.nginx_server_config.read_bytes()
    except OSError:
        raise CloudDeployError("nginx_server_configuration_unavailable") from None
    if (
        spec.nginx_server_config.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= len(payload) <= 2 * 1024 * 1024
    ):
        raise CloudDeployError("nginx_server_configuration_invalid")
    count = payload.count(f"{LEGACY_POINTER_LOCATION_HEADER} {{".encode("ascii"))
    if count > 1:
        raise CloudDeployError("nginx_legacy_admin_route_invalid")
    return count == 1


def _seed_legacy_public_bootstrap_pointer(
    spec: CloudDeploymentSpec,
) -> _PublicBootstrapSeedIdentity:
    """Seed the CP object before retiring the only live exact Nginx route.

    A pre-existing target is accepted only when it is the exact validated
    source bytes. That fail-closed CAS rule prevents a legacy publisher and a
    partially started Control Plane from becoming competing authorities.
    """

    legacy_route = _legacy_pointer_route_present(spec)
    if legacy_route:
        source = _read_public_bootstrap_file(
            LEGACY_PUBLIC_BOOTSTRAP_INDEX_PATH,
            code="legacy_public_bootstrap_seed_unavailable",
        )
        _validate_public_bootstrap_seed_payload(spec, source)
    else:
        source = None
    target_exists = os.path.lexists(PUBLIC_BOOTSTRAP_INDEX_PATH)
    if target_exists:
        target = _read_public_bootstrap_file(
            PUBLIC_BOOTSTRAP_INDEX_PATH,
            code="public_bootstrap_seed_target_invalid",
        )
        _validate_public_bootstrap_seed_payload(spec, target)
        if source is not None and target != source:
            raise CloudDeployError("public_bootstrap_seed_conflict")
        payload = target
    else:
        if source is None:
            source = (
                _canonical_json(unpublished_public_bootstrap_index()) + b"\n"
            )
            _validate_public_bootstrap_seed_payload(spec, source)
        _atomic_write(PUBLIC_BOOTSTRAP_INDEX_PATH, source, mode=0o644)
        try:
            shutil.chown(
                PUBLIC_BOOTSTRAP_INDEX_PATH,
                user="ecorex-cloud",
                group="ecorex-storage",
            )
            os.chmod(PUBLIC_BOOTSTRAP_INDEX_PATH, 0o644)
            _fsync_directory(PUBLIC_BOOTSTRAP_ROOT)
        except (LookupError, OSError):
            raise CloudDeployError("public_bootstrap_seed_write_failed") from None
        payload = _read_public_bootstrap_file(
            PUBLIC_BOOTSTRAP_INDEX_PATH,
            code="public_bootstrap_seed_write_failed",
        )
        if payload != source:
            raise CloudDeployError("public_bootstrap_seed_write_failed")
        _validate_public_bootstrap_seed_payload(spec, payload)
    return _PublicBootstrapSeedIdentity(
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        legacy_exact_route=legacy_route,
    )


def _verify_public_bootstrap_seed_before_route_retire(
    spec: CloudDeploymentSpec, seed: _PublicBootstrapSeedIdentity
) -> None:
    if not isinstance(seed, _PublicBootstrapSeedIdentity):
        raise CloudDeployError("public_bootstrap_seed_identity_invalid")
    route_present = _legacy_pointer_route_present(spec)
    if route_present != seed.legacy_exact_route:
        raise CloudDeployError("public_bootstrap_seed_identity_changed")
    target = _read_public_bootstrap_file(
        PUBLIC_BOOTSTRAP_INDEX_PATH,
        code="public_bootstrap_seed_target_invalid",
    )
    _validate_public_bootstrap_seed_payload(spec, target)
    if (
        target != seed.payload
        or len(target) != seed.size_bytes
        or hashlib.sha256(target).hexdigest() != seed.sha256
    ):
        raise CloudDeployError("public_bootstrap_seed_identity_changed")
    if route_present:
        legacy = _read_public_bootstrap_file(
            LEGACY_PUBLIC_BOOTSTRAP_INDEX_PATH,
            code="legacy_public_bootstrap_seed_unavailable",
        )
        _validate_public_bootstrap_seed_payload(spec, legacy)
        if (
            legacy != seed.payload
            or len(legacy) != seed.size_bytes
            or hashlib.sha256(legacy).hexdigest() != seed.sha256
        ):
            raise CloudDeployError("legacy_public_bootstrap_seed_changed")


def _encoded_public_keyring(
    value: Any,
    *,
    code: str,
) -> tuple[dict[str, str], dict[str, bytes]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            raise CloudDeployError(code) from None
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 32:
        raise CloudDeployError(code)
    encoded: dict[str, str] = {}
    raw: dict[str, bytes] = {}
    for key_id, public_value in value.items():
        if (
            not isinstance(key_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id) is None
            or not isinstance(public_value, str)
        ):
            raise CloudDeployError(code)
        try:
            public = base64.b64decode(public_value, validate=True)
        except (TypeError, ValueError):
            raise CloudDeployError(code) from None
        if len(public) != 32 or base64.b64encode(public).decode("ascii") != public_value:
            raise CloudDeployError(code)
        encoded[key_id] = public_value
        raw[key_id] = public
    return encoded, raw


def _public_pointer_keyring_material(
    spec: CloudDeploymentSpec,
) -> tuple[dict[str, str], dict[str, bytes], dict[str, str], dict[str, bytes]]:
    try:
        environment = _parse_env(
            SECRET_ROOT / "control-plane.secret.env",
            secret=True,
        )
        release_encoded, release_raw = _encoded_public_keyring(
            environment.get("ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON"),
            code="public_pointer_release_keyring_invalid",
        )
        publication_encoded, publication_raw = _encoded_public_keyring(
            environment.get("ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON"),
            code="public_pointer_publication_keyring_invalid",
        )
        configured_release, _configured_raw = _encoded_public_keyring(
            _read_json(spec.release_keyring_path, "release_keyring_invalid"),
            code="release_keyring_invalid",
        )
    except CloudDeployError:
        raise
    if release_encoded != configured_release:
        raise CloudDeployError("public_pointer_release_keyring_mismatch")
    if set(release_raw).intersection(publication_raw) or set(
        release_raw.values()
    ).intersection(publication_raw.values()):
        raise CloudDeployError("public_pointer_trust_roles_overlap")
    return release_encoded, release_raw, publication_encoded, publication_raw


def _public_pointer_keyrings(
    spec: CloudDeploymentSpec,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    _, release_raw, _, publication_raw = _public_pointer_keyring_material(spec)
    return release_raw, publication_raw


def _install_publication_keyring(spec: CloudDeploymentSpec) -> None:
    """Materialize only public freshness keys for the root site authority."""

    _, _, publication_encoded, _ = _public_pointer_keyring_material(spec)
    _atomic_write(
        PUBLICATION_KEYRING_PATH,
        _canonical_json(publication_encoded) + b"\n",
        mode=0o644,
    )


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
        or result.stdout.count(
            b"location = /ecorex-agent/public-bootstrap-index.json"
        )
        != 1
        or b"alias /srv/ecorex-agent-download/public-pointer/"
        b"public-bootstrap-index.json;" not in result.stdout
        or b"alias /srv/ecorex-agent-download/current/"
        b"public-bootstrap-index.json;" in result.stdout
    ):
        raise CloudDeployError("nginx_route_not_wired")


def _write_slot_environment(slot: str, release: Path) -> None:
    _prepare_slot_runtime_directory(slot)
    ports = PORTS[slot]
    values = {
        "control-plane": {
            "ECOREX_CP_BIND_HOST": "127.0.0.1",
            "ECOREX_CP_BIND_PORT": str(ports["control_plane"]),
            "ECOREX_CP_INSTANCE_ID": f"ecorex-cloud-{slot}",
            "ECOREX_CP_RELEASE_REPLICA_NAMESPACE": f"v{PRODUCT_VERSION}",
            "ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION": PRODUCT_VERSION,
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
        "image-worker": {
            "ECOREX_IMAGE_BIND_HOST": "127.0.0.1",
            "ECOREX_IMAGE_BIND_PORT": str(ports["image_worker"]),
            "ECOREX_IMAGE_INSTANCE_ID": f"ecorex-image-worker-{slot}",
        },
    }
    for service, environment in values.items():
        payload = "".join(f"{name}={value}\n" for name, value in environment.items())
        _atomic_write(
            CONFIG_ROOT / "slots" / slot / f"{service}.env",
            payload.encode("ascii"),
        )
    _atomic_symlink(release, SLOT_ROOT / slot / "current")


def _prepare_slot_runtime_directory(slot: str) -> None:
    """Make the fixed slot traversable only by root and the service group."""

    if slot not in SLOTS:
        raise CloudDeployError("slot_runtime_directory_invalid")
    directory = SLOT_ROOT / slot
    try:
        SLOT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
        root_metadata = SLOT_ROOT.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or SLOT_ROOT.resolve(strict=True) != SLOT_ROOT
        ):
            raise OSError
        directory.mkdir(mode=0o750, exist_ok=True)
        metadata = directory.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or directory.resolve(strict=True) != directory
            or metadata.st_dev != root_metadata.st_dev
        ):
            raise OSError
        shutil.chown(directory, user="root", group="ecorex-cloud")
        os.chmod(directory, 0o750)
        _fsync_directory(directory)
        _fsync_directory(SLOT_ROOT)
    except OSError:
        raise CloudDeployError("slot_runtime_directory_invalid") from None


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
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    arguments: dict[str, Any] = {
        "code": code,
        "environment": environment,
        "timeout": timeout,
    }
    if input_bytes is not None:
        arguments["input_bytes"] = input_bytes
    return _run(
        [
            "/usr/sbin/runuser",
            "--user",
            "ecorex-cloud",
            "--preserve-environment",
            "--",
            *command,
        ],
        **arguments,
    )


def _schema_gate(release: Path, slot: str) -> None:
    """Apply idempotent storage migrations before legacy model import.

    The image provider's dynamic model configuration is authoritative in the
    Control Plane.  On a first v0.2.9.2 migration that configuration does not
    exist until the legacy Admin import is committed, so provider readiness is
    deliberately checked in the post-import contract gate below.
    """

    for service in _SERVICE_MODULES:
        environment = _service_environment(service, slot)
        _run_service_command(
            _service_command(release, service, "schema", "migrate"),
            code=f"{service.replace('-', '_')}_schema_migration_failed",
            environment=environment,
            timeout=600,
        )


def _production_contract_gate(release: Path, slot: str) -> None:
    """Validate all live provider/storage contracts after model import."""

    for service in _SERVICE_MODULES:
        environment = _service_environment(service, slot)
        # These checks intentionally execute the real S3 control/write/read/
        # delete probes.  MinIO is never accepted on API-compatibility faith.
        _run_service_command(
            _service_command(release, service, "schema", "check"),
            code=f"{service.replace('-', '_')}_production_contract_failed",
            environment=environment,
            timeout=600,
        )


_RECOVERY_SCHEMA_SERVICES = (
    ("control-plane", "control-plane", "control-plane"),
    ("gateway", "gateway", "gateway"),
    ("image-api", "image", "image"),
    ("image-worker", "image", "image-worker"),
)


def _recovery_schema_check(
    release: Path, slot: str, *, source: bool
) -> None:
    for service_name, runtime_service, environment_service in _RECOVERY_SCHEMA_SERVICES:
        environment = _service_environment(environment_service, slot)
        try:
            _run_service_command(
                _service_command(release, runtime_service, "schema", "check"),
                code=f"{service_name.replace('-', '_')}_recovery_schema_incompatible",
                environment=environment,
                timeout=600,
            )
        except CloudDeployError:
            if source:
                raise _RecoverySourceSchemaIncompatible(
                    "recovery_source_schema_incompatible"
                ) from None
            raise CloudDeployError("recovery_target_schema_incompatible") from None


def _unit(service: str, slot: str) -> str:
    return f"{service}@{slot}.service"


def _systemctl(spec: CloudDeploymentSpec, verb: str, units: Iterable[str]) -> None:
    command = [str(spec.systemctl_binary), verb, *units]
    _run(command, code=f"systemd_{verb.replace('-', '_')}_failed", timeout=180)


def _slot_units(slot: str) -> list[str]:
    return [_unit(service, slot) for service in SERVICE_NAMES]


def _slot_api_units(slot: str) -> list[str]:
    return [_unit(service, slot) for service in API_SERVICE_NAMES]


def _stop_target_services(
    spec: CloudDeploymentSpec, state: Mapping[str, Any] | None
) -> None:
    # Once a v1 slot is authoritative, subsequent v1-to-v1 transitions only
    # fence the two v1 writer sets.  The retained v0.2.9.2 Web compatibility
    # service is deliberately not a member of that transition and therefore
    # remains available to users who have not upgraded yet.
    units: Iterable[str] = (
        reversed(_slot_units(str(state["active_slot"])))
        if state is not None
        else reversed(LEGACY_SERVICE_NAMES)
    )
    _systemctl(spec, "stop", units)


def _start_target_services(
    spec: CloudDeploymentSpec, state: Mapping[str, Any] | None
) -> None:
    if state is None:
        _systemctl(spec, "start", LEGACY_SERVICE_NAMES)
        _systemctl(spec, "is-active", LEGACY_SERVICE_NAMES)
        return
    slot = str(state["active_slot"])
    _prepare_slot_runtime_directory(slot)
    units = _slot_units(slot)
    try:
        _systemctl(spec, "start", _slot_api_units(slot))
        _wait_api_health(spec, slot)
        _systemctl(spec, "start", (_unit(IMAGE_WORKER_SERVICE_NAME, slot),))
        _wait_worker_health(spec, slot)
        _wait_health(spec, slot)
    except CloudDeployError:
        # Recovery may be re-entered repeatedly. A candidate that cannot
        # become healthy must never remain in systemd's restart loop while
        # the still-authoritative source serves traffic.
        _systemctl(spec, "stop", reversed(units))
        raise


def _stop_transition_writers(
    spec: CloudDeploymentSpec, journal: Mapping[str, Any]
) -> None:
    # Stop the inactive/partial target first, then the authoritative source.
    # Starting either side is forbidden until both stop operations complete.
    _stop_target_services(spec, journal["target_state"])
    _stop_target_services(spec, journal["source_state"])


def _legacy_target_environment(slot: str) -> tuple[Path, bytes, dict[str, str]]:
    environment = _service_environment("control-plane", slot)
    target = Path(str(environment.get("ECOREX_CP_DATABASE_PATH", "")))
    if target != CONTROL_PLANE_DATABASE_PATH:
        raise CloudDeployError("legacy_admin_target_outside_fence")
    encoded = environment.get("ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64", "")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise CloudDeployError("legacy_admin_encryption_key_invalid") from None
    if len(key) != 32:
        raise CloudDeployError("legacy_admin_encryption_key_invalid")
    return target, key, environment


def _commit_legacy_password_credentials(slot: str) -> None:
    """Merge legacy password hashes into already-existing live v1 accounts.

    This import is intentionally independent from the first-install Admin
    importer: upgrades from an occupied v1 database must gain credentials
    without recreating users, while deleted legacy accounts must stay deleted.
    """

    if not LEGACY_ADMIN_DATABASE_PATH.exists():
        return
    if LEGACY_ADMIN_DATABASE_PATH.is_symlink():
        raise CloudDeployError("legacy_password_import_failed")
    target, _key, _environment = _legacy_target_environment(slot)
    try:
        import_v0292_password_credentials(
            LEGACY_ADMIN_DATABASE_PATH,
            target,
            dry_run=False,
        )
    except LegacyPasswordCredentialImportError:
        raise CloudDeployError("legacy_password_import_failed") from None


def _legacy_identity_payload(records: Sequence[Mapping[str, object]]) -> bytes:
    payload = b"".join(_canonical_json(dict(record)) + b"\n" for record in records)
    if len(payload) > 8 * 1024 * 1024 or len(records) > 100_000:
        raise CloudDeployError("legacy_identity_import_oversized")
    return payload


def _prepare_legacy_import_contract(
    journal: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[dict[str, object], ...]]:
    contract = journal.get("legacy_admin_migration")
    if contract is None:
        return dict(journal), ()
    normalized = _normalize_legacy_migration_contract(
        contract, phase=str(journal.get("phase"))
    )
    assert normalized is not None
    cutoff = _parse_utc_text(normalized["as_of"])
    try:
        report = import_v0292_admin_management(
            LEGACY_ADMIN_DATABASE_PATH,
            CONTROL_PLANE_DATABASE_PATH,
            encryption_key=None,
            dry_run=True,
            as_of=cutoff,
        )
        records, identity_report = export_v0292_legacy_identities(
            LEGACY_ADMIN_DATABASE_PATH,
            as_of=cutoff,
        )
        source_digest_after = _sha256_file(LEGACY_ADMIN_DATABASE_PATH)
    except (LegacyAdminManagementImportError, LegacyIdentityExportError):
        raise CloudDeployError("legacy_admin_migration_inventory_failed") from None
    observed = {
        "source_version": "0.2.9.2",
        "as_of": normalized["as_of"],
        "source_database_sha256": report.source_file_sha256,
        "source_snapshot_sha256": report.source_snapshot_sha256,
        "import_receipt_sha256": report.import_receipt_sha256,
        "identity_records_sha256": identity_report.records_sha256,
    }
    if source_digest_after != report.source_file_sha256:
        raise CloudDeployError("legacy_admin_source_changed")
    declared = tuple(normalized[key] for key in (
        "source_database_sha256",
        "source_snapshot_sha256",
        "import_receipt_sha256",
        "identity_records_sha256",
    ))
    if any(item is not None for item in declared):
        if dict(normalized) != observed:
            raise CloudDeployError("legacy_admin_migration_identity_changed")
        return dict(journal), records
    updated = dict(journal)
    updated["legacy_admin_migration"] = observed
    return _write_transition_journal(updated), records


def _commit_legacy_admin_and_identity(
    release: Path,
    slot: str,
    journal: Mapping[str, Any],
    records: Sequence[Mapping[str, object]],
) -> None:
    contract = _normalize_legacy_migration_contract(
        journal.get("legacy_admin_migration"), phase="legacy_imported"
    )
    if contract is None:
        return
    target, key, environment = _legacy_target_environment(slot)
    cutoff = _parse_utc_text(contract["as_of"])
    try:
        report = import_v0292_admin_management(
            LEGACY_ADMIN_DATABASE_PATH,
            target,
            encryption_key=key,
            dry_run=False,
            as_of=cutoff,
        )
    except LegacyAdminManagementImportError:
        raise CloudDeployError("legacy_admin_import_failed") from None
    if (
        report.source_file_sha256 != contract["source_database_sha256"]
        or report.source_snapshot_sha256 != contract["source_snapshot_sha256"]
        or report.import_receipt_sha256 != contract["import_receipt_sha256"]
    ):
        raise CloudDeployError("legacy_admin_migration_identity_changed")
    # The first-install importer creates the target users in the operation
    # above. A second idempotent password pass can now attach credentials.
    _commit_legacy_password_credentials(slot)
    _ensure_configured_deployment_platform_admin(
        target,
        encryption_key=key,
        environment=environment,
    )
    payload = _legacy_identity_payload(
        _eligible_legacy_identity_records(
            records,
            target=target,
            encryption_key=key,
        )
    )
    if payload:
        _run_service_command(
            _service_command(release, "control-plane", "device", "legacy-import"),
            code="legacy_identity_import_failed",
            environment=environment,
            timeout=600,
            input_bytes=payload,
        )


def _eligible_legacy_identity_records(
    records: Sequence[Mapping[str, object]],
    *,
    target: Path,
    encryption_key: bytes,
) -> tuple[Mapping[str, object], ...]:
    """Keep legacy credentials only for live, currently usable accounts.

    A v0.2.9.2 credential can outlive its user record.  Importing that mapping
    would revive a deleted identity or make the all-or-nothing device import
    fail.  Credentials also cannot produce a valid device lease until an
    administrator has an active public model catalog.  Preserve neither kind
    of unusable mapping; users, conversations and project data are migrated
    independently by their own authoritative paths.
    """

    try:
        repository = AdminManagementRepository(target, encryption_key=encryption_key)
        catalog = repository.active_public_catalog()
    except Exception:
        raise CloudDeployError("legacy_identity_account_inventory_failed") from None
    if not catalog:
        return ()

    eligible: list[Mapping[str, object]] = []
    for record in records:
        account_id = record.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            raise CloudDeployError("legacy_identity_record_invalid")
        try:
            user = repository.get_user(account_id)
        except AdminManagementNotFound:
            continue
        except Exception:
            raise CloudDeployError("legacy_identity_account_inventory_failed") from None
        if user.status == "active":
            eligible.append(record)
    return tuple(eligible)


def _ensure_configured_deployment_platform_admin(
    target: Path,
    *,
    encryption_key: bytes,
    environment: Mapping[str, str],
) -> None:
    """Create the deployment-owned admin only when Device Identity selects it.

    v0.2.9.2 can contain no active administrator while still having valid user
    sessions.  Legacy session import needs a live platform-admin allowlist
    before the device broker can compose, but creating that account before the
    management import would violate the importer's empty-business-data fence.
    The transaction therefore runs immediately after the idempotent import.
    """

    raw = environment.get("ECOREX_CP_DEVICE_PLATFORM_ADMIN_ACCOUNT_IDS", "")
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    if _DEPLOYMENT_PLATFORM_ADMIN_ACCOUNT_ID not in selected:
        return
    try:
        repository = AdminManagementRepository(target, encryption_key=encryption_key)
        try:
            user = repository.get_user(_DEPLOYMENT_PLATFORM_ADMIN_ACCOUNT_ID)
        except AdminManagementNotFound:
            repository.create_user(
                CreateAdminUserRequest(
                    account_id=_DEPLOYMENT_PLATFORM_ADMIN_ACCOUNT_ID,
                    display_name=_DEPLOYMENT_PLATFORM_ADMIN_DISPLAY_NAME,
                    email=None,
                    organization_id=_DEPLOYMENT_PLATFORM_ADMIN_ORGANIZATION_ID,
                    token_limit=0,
                    image_limit=0,
                    client_request_id="bootstrap-platform-admin-"
                    + hashlib.sha256(
                        _DEPLOYMENT_PLATFORM_ADMIN_ACCOUNT_ID.encode("utf-8")
                    ).hexdigest(),
                ),
                actor=_DEPLOYMENT_PLATFORM_ADMIN_ACTOR,
            )
            return
    except Exception:
        raise CloudDeployError("deployment_platform_admin_bootstrap_failed") from None
    if (
        user.status != "active"
        or user.display_name != _DEPLOYMENT_PLATFORM_ADMIN_DISPLAY_NAME
        or user.organization_id != _DEPLOYMENT_PLATFORM_ADMIN_ORGANIZATION_ID
    ):
        raise CloudDeployError("deployment_platform_admin_conflict")


def _legacy_admin_import_committed(journal: Mapping[str, Any]) -> bool:
    contract = journal.get("legacy_admin_migration")
    if contract is None:
        return False
    normalized = _normalize_legacy_migration_contract(
        contract, phase=str(journal.get("phase"))
    )
    if normalized is None or normalized["import_receipt_sha256"] is None:
        return False
    target = CONTROL_PLANE_DATABASE_PATH
    try:
        if target.is_symlink() or not target.is_file():
            return False
        connection = sqlite3.connect(
            f"{target.as_uri()}?mode=ro&nofollow=1",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT operation,response_json FROM admin_ops_idempotency "
            "WHERE actor_subject=? AND client_request_id=?",
            (
                "migration:v0.2.9.2",
                "legacy-admin-" + str(normalized["import_receipt_sha256"])[:32],
            ),
        ).fetchone()
        connection.close()
    except (OSError, sqlite3.Error):
        raise CloudDeployError("legacy_admin_import_receipt_unavailable") from None
    if row is None:
        return False
    try:
        payload = json.loads(str(row["response_json"]))
    except (TypeError, json.JSONDecodeError):
        raise CloudDeployError("legacy_admin_import_receipt_invalid") from None
    if (
        row["operation"] != "legacy.v0292.admin-management.import"
        or not isinstance(payload, Mapping)
        or payload.get("import_receipt_sha256")
        != normalized["import_receipt_sha256"]
    ):
        raise CloudDeployError("legacy_admin_import_receipt_invalid")
    return True


def _ensure_activation_schema_ready(
    spec: CloudDeploymentSpec,
    journal: Mapping[str, Any],
    *,
    target_release: Path | None = None,
) -> dict[str, Any]:
    """Durably fence writers before an idempotent target migration.

    ``migrating`` is written before the first stop request.  A crash at any
    later instruction therefore leaves enough evidence for startup recovery
    to stop both writer sets and rerun the target migration safely.
    """

    effective = dict(journal)
    if effective.get("operation") != "activate":
        return effective
    phase = str(effective.get("phase"))
    if phase in {"schema_ready", "target_ready", "routes_switched", "state_written"}:
        return effective
    if phase == "prepared":
        effective = _advance_transition_journal(effective, "migrating")
        phase = "migrating"
    if phase == "legacy_imported":
        _stop_transition_writers(spec, effective)
        return _advance_transition_journal(effective, "schema_ready")
    if phase != "migrating":
        raise CloudDeployError("activation_journal_invalid")
    target_state = effective.get("target_state")
    if not isinstance(target_state, Mapping):
        raise CloudDeployError("activation_journal_invalid")
    _stop_transition_writers(spec, effective)
    release = (
        target_release
        if target_release is not None
        else _verify_transition_release(spec, target_state)
    )
    _schema_gate(release, str(target_state["active_slot"]))
    # This also runs on v1-to-v1 upgrades whose target database already has
    # business data and therefore cannot use the full legacy importer.
    _commit_legacy_password_credentials(str(target_state["active_slot"]))
    effective, records = _prepare_legacy_import_contract(effective)
    if effective.get("legacy_admin_migration") is not None:
        _commit_legacy_admin_and_identity(
            release,
            str(target_state["active_slot"]),
            effective,
            records,
        )
        effective = _advance_transition_journal(effective, "legacy_imported")
    _production_contract_gate(release, str(target_state["active_slot"]))
    return _advance_transition_journal(effective, "schema_ready")


def _wait_endpoints(
    endpoints: Sequence[str], *, timeout_seconds: float
) -> None:
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
            return
        time.sleep(1.0)
    raise CloudDeployError("candidate_readiness_failed")


def _slot_health_endpoints(slot: str) -> tuple[str, str, str, str]:
    ports = PORTS[slot]
    return (
        f"http://127.0.0.1:{ports['control_plane']}/health/ready",
        f"http://127.0.0.1:{ports['gateway']}/health/ready",
        f"http://127.0.0.1:{ports['image']}/health/ready",
        f"http://127.0.0.1:{ports['image_worker']}/health/ready",
    )


def _wait_api_health(
    spec: CloudDeploymentSpec, slot: str, *, timeout_seconds: float = 120.0
) -> None:
    del spec
    _wait_endpoints(
        _slot_health_endpoints(slot)[:3],
        timeout_seconds=timeout_seconds,
    )


def _wait_worker_health(
    spec: CloudDeploymentSpec, slot: str, *, timeout_seconds: float = 120.0
) -> None:
    del spec
    _wait_endpoints(
        _slot_health_endpoints(slot)[3:],
        timeout_seconds=timeout_seconds,
    )


def _wait_health(
    spec: CloudDeploymentSpec, slot: str, *, timeout_seconds: float = 120.0
) -> None:
    _wait_endpoints(
        _slot_health_endpoints(slot),
        timeout_seconds=timeout_seconds,
    )
    _run(
        [
            str(spec.systemctl_binary),
            "is-active",
            _unit(IMAGE_WORKER_SERVICE_NAME, slot),
        ],
        code="image_worker_unavailable",
    )


def _nginx_target(slot: str) -> Path:
    return NGINX_ROOT / f"control-plane-{slot}.conf"


def _switch_nginx_targets(
    spec: CloudDeploymentSpec, *, control_target: Path, admin_target: Path
) -> None:
    control_link = NGINX_ROOT / "active-control-plane.conf"
    admin_link = NGINX_ROOT / "active-admin-route.conf"
    allowed_control = {
        _nginx_target("blue"),
        _nginx_target("green"),
        NGINX_ROOT / "control-plane-disabled.conf",
    }
    allowed_admin = {
        NGINX_ROOT / "admin-route-control-plane.conf",
        NGINX_ROOT / "admin-route-legacy.conf",
    }
    if control_target not in allowed_control or admin_target not in allowed_admin:
        raise CloudDeployError("nginx_route_switch_invalid")
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
        _atomic_symlink(control_target, control_link)
        _atomic_symlink(admin_target, admin_link)
        _fsync_directory(NGINX_ROOT)
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
            _fsync_directory(NGINX_ROOT)
            _run([str(spec.nginx_binary), "-t"], code="nginx_restore_failed")
            _run(
                [str(spec.systemctl_binary), "reload", "nginx.service"],
                code="nginx_restore_failed",
            )
        if isinstance(error, CloudDeployError):
            raise
        raise CloudDeployError("nginx_route_switch_failed") from None


def _switch_nginx(spec: CloudDeploymentSpec, slot: str) -> None:
    if slot not in SLOTS:
        raise CloudDeployError("nginx_route_switch_invalid")
    _switch_nginx_targets(
        spec,
        control_target=_nginx_target(slot),
        admin_target=NGINX_ROOT / "admin-route-control-plane.conf",
    )


def _switch_nginx_legacy(spec: CloudDeploymentSpec) -> None:
    _switch_nginx_targets(
        spec,
        control_target=NGINX_ROOT / "control-plane-disabled.conf",
        admin_target=NGINX_ROOT / "admin-route-legacy.conf",
    )


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
        "active_target_type": "slot",
        "active_release_id": release_id,
        "active_slot": slot,
        "previous_target_type": "legacy" if prior is None else "slot",
        "previous_release_id": None if prior is None else prior["active_release_id"],
        "previous_slot": None if prior is None else prior["active_slot"],
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "activated_at_unix": int(time.time()),
    }


def _rollback_slot_state(
    current: Mapping[str, Any], *, release_id: str, slot: str, artifact_manifest_sha256: str
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "active_target_type": "slot",
        "active_release_id": release_id,
        "active_slot": slot,
        "previous_target_type": "slot",
        "previous_release_id": current["active_release_id"],
        "previous_slot": current["active_slot"],
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "activated_at_unix": int(time.time()),
    }


def _legacy_rollback_receipt(current: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "active_target_type": "legacy",
        "active_release_id": None,
        "active_slot": None,
        "previous_target_type": "slot",
        "previous_release_id": current["active_release_id"],
        "previous_slot": current["active_slot"],
        "artifact_manifest_sha256": None,
        "activated_at_unix": int(time.time()),
    }


def _new_transition_journal(
    *,
    operation: str,
    source_state: Mapping[str, Any] | None,
    target_state: Mapping[str, Any] | None,
    legacy_admin_migration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if operation not in TRANSITION_OPERATIONS:
        raise CloudDeployError("activation_journal_invalid")
    normalized_source = (
        None
        if source_state is None
        else _normalized_slot_state(source_state, "activation_journal_invalid")
    )
    normalized_target = (
        None
        if target_state is None
        else _normalized_slot_state(target_state, "activation_journal_invalid")
    )
    return _write_transition_journal(
        {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "phase": "prepared",
            "source_target_type": "legacy" if normalized_source is None else "slot",
            "source_state": normalized_source,
            "target_target_type": "legacy" if normalized_target is None else "slot",
            "target_state": normalized_target,
            "created_at_unix": int(time.time()),
            "legacy_admin_migration": _normalize_legacy_migration_contract(
                legacy_admin_migration, phase="prepared"
            ),
        }
    )


def _release_for_state(state: Mapping[str, Any]) -> Path:
    release = RELEASE_ROOT / str(state["active_release_id"])
    try:
        if release.is_symlink() or not release.is_dir():
            raise OSError
        release.relative_to(RELEASE_ROOT)
    except (OSError, ValueError):
        raise CloudDeployError("rollback_release_missing") from None
    return release


def _verify_transition_release(
    spec: CloudDeploymentSpec, state: Mapping[str, Any]
) -> Path:
    release = _release_for_state(state)
    identity = _release_directory_identity(release)
    manifest = release / "cloud-release-manifest.json"
    digest = _sha256_file(manifest)
    declared = state.get("artifact_manifest_sha256")
    if declared is not None and declared != digest:
        raise CloudDeployError("artifact_manifest_digest_mismatch")
    manifest_value = _read_json(manifest, "artifact_manifest_invalid")
    if not isinstance(manifest_value, Mapping):
        raise CloudDeployError("artifact_manifest_invalid")
    source_commit = manifest_value.get("source_commit")
    dependency_lock_manifest_sha256 = manifest_value.get(
        "dependency_lock_manifest_sha256"
    )
    if not isinstance(source_commit, str) or re.fullmatch(
        r"[0-9a-f]{40}", source_commit
    ) is None:
        raise CloudDeployError("artifact_manifest_invalid")
    if (
        not isinstance(dependency_lock_manifest_sha256, str)
        or SHA256.fullmatch(dependency_lock_manifest_sha256) is None
    ):
        raise CloudDeployError("artifact_manifest_invalid")
    _product_version_key(manifest_value.get("version"))
    staged_spec = dataclasses.replace(
        spec,
        release_id=str(state["active_release_id"]),
        source_commit=source_commit,
        dependency_lock_manifest_sha256=dependency_lock_manifest_sha256,
        artifact_root=release,
        artifact_manifest_sha256=digest,
    )
    _validate_artifact(staged_spec, historical_release=True)
    _seal_release_directory(release, identity)
    _verify_staged_runtime(release)
    return release


def _write_slot_projection(state: Mapping[str, Any]) -> None:
    normalized = _normalized_slot_state(state, "deployment_state_invalid")
    release = _release_for_state(normalized)
    _atomic_write(
        STATE_ROOT / "active.json",
        _canonical_json(normalized) + b"\n",
        mode=0o600,
    )
    _fsync_directory(STATE_ROOT)
    _atomic_symlink(release, INSTALL_ROOT / "current")
    _fsync_directory(INSTALL_ROOT)


def _remove_slot_projection() -> None:
    active = STATE_ROOT / "active.json"
    current = INSTALL_ROOT / "current"
    try:
        if active.exists() or active.is_symlink():
            metadata = active.lstat()
            if active.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise OSError
            active.unlink()
            _fsync_directory(STATE_ROOT)
        if current.exists() or current.is_symlink():
            if not current.is_symlink():
                raise OSError
            current.unlink()
            _fsync_directory(INSTALL_ROOT)
    except OSError:
        raise CloudDeployError("deployment_state_clear_failed") from None


def _route_targets(state: Mapping[str, Any] | None) -> tuple[Path, Path]:
    if state is None:
        return (
            NGINX_ROOT / "control-plane-disabled.conf",
            NGINX_ROOT / "admin-route-legacy.conf",
        )
    return (
        _nginx_target(str(state["active_slot"])),
        NGINX_ROOT / "admin-route-control-plane.conf",
    )


def _classify_transition_routes(journal: Mapping[str, Any]) -> str:
    """Classify the two live Nginx pointers without trusting a journal phase."""

    control_link = NGINX_ROOT / "active-control-plane.conf"
    admin_link = NGINX_ROOT / "active-admin-route.conf"
    try:
        if not control_link.is_symlink() or not admin_link.is_symlink():
            return "unknown"
        actual = (
            control_link.resolve(strict=True),
            admin_link.resolve(strict=True),
        )
    except OSError:
        return "unknown"
    source = _route_targets(journal["source_state"])
    target = _route_targets(journal["target_state"])
    if actual == source:
        return "source"
    if actual == target:
        return "target"
    expected = {source[0], source[1], target[0], target[1]}
    if actual[0] in expected or actual[1] in expected:
        return "partial"
    return "unknown"


def _transition_resolution(journal: Mapping[str, Any]) -> str:
    phase = str(journal["phase"])
    route_state = _classify_transition_routes(journal)
    if phase in {"legacy_imported", "routes_switched", "state_written"}:
        return "target"
    # Before the durable routes_switched marker, route identity may nominate
    # the source, but recovery still schema-checks all four service roles. A
    # target, mixed or unknown pair may already have accepted writes and must
    # roll forward immediately.
    return "source" if route_state == "source" else "target"


def _restore_transition_source(
    spec: CloudDeploymentSpec, journal: Mapping[str, Any]
) -> None:
    if (
        journal.get("operation") == "activate"
        and journal.get("legacy_admin_migration") is not None
        and _legacy_admin_import_committed(journal)
    ):
        raise _RecoverySourceSchemaIncompatible(
            "recovery_source_forbidden_after_target_write"
        )
    source_state = journal["source_state"]
    target_state = journal["target_state"]
    # Recovery never checks or starts either side while the other side may
    # still be writing.  Repeated stop requests are intentionally idempotent.
    _stop_transition_writers(spec, journal)
    if source_state is not None:
        source_release = _verify_transition_release(spec, source_state)
        _recovery_schema_check(
            source_release, str(source_state["active_slot"]), source=True
        )
    elif target_state is not None and journal.get("operation") == "activate":
        # The first v1 migration writes only the new encrypted v1 databases;
        # the released legacy stores are immutable migration inputs.  Legacy
        # writers were stopped before migration began, so restarting them is a
        # safe availability fallback even when a target migration was partial.
        # Later v1-to-v1 transitions have a concrete source_state and must pass
        # the four-role schema compatibility check above.
        pass
    _start_target_services(spec, source_state)
    if source_state is not None:
        _switch_nginx(spec, str(source_state["active_slot"]))
        _write_slot_projection(source_state)
    else:
        _switch_nginx_legacy(spec)
        _remove_slot_projection()
    _clear_transition_journal()


def _complete_transition_target(
    spec: CloudDeploymentSpec, journal: Mapping[str, Any]
) -> None:
    source_state = journal["source_state"]
    target_state = journal["target_state"]
    if journal.get("operation") == "activate" and journal.get("phase") not in {
        "schema_ready",
        "target_ready",
        "routes_switched",
        "state_written",
    }:
        raise CloudDeployError("activation_schema_not_ready")
    _stop_transition_writers(spec, journal)
    if target_state is not None:
        target_release = _verify_transition_release(spec, target_state)
        _recovery_schema_check(
            target_release, str(target_state["active_slot"]), source=False
        )
    _start_target_services(spec, target_state)
    if target_state is not None:
        _switch_nginx(spec, str(target_state["active_slot"]))
        _write_slot_projection(target_state)
    else:
        _switch_nginx_legacy(spec)
        _remove_slot_projection()
    _clear_transition_journal()


def _resolve_pending_transition(
    spec: CloudDeploymentSpec, journal: Mapping[str, Any]
) -> str:
    effective = dict(journal)
    resolution = _transition_resolution(effective)
    if (
        effective.get("operation") == "activate"
        and effective.get("phase") == "migrating"
        and effective.get("legacy_admin_migration") is not None
        and _legacy_admin_import_committed(effective)
    ):
        # The business-write receipt is the crash-safe authority for the
        # narrow window between SQLite COMMIT and the next journal fsync.
        # Once present, restarting legacy would create two authorities.
        resolution = "target"
    if (
        effective.get("operation") == "activate"
        and effective.get("phase") == "migrating"
        and resolution == "source"
    ):
        # A crash or deterministic migration failure must not strand a known
        # compatible source offline.  Restore it first; only an incompatible
        # v1 source forces the idempotent target migration to continue.
        try:
            _restore_transition_source(spec, effective)
            return "source"
        except _RecoverySourceSchemaIncompatible:
            resolution = "target"
    if effective.get("operation") == "activate" and resolution == "target":
        effective = _ensure_activation_schema_ready(spec, effective)
        # Recovery direction is monotonic. A target decision can be caused by
        # accepted target writes even while Nginx still points at the source;
        # recalculating from routes here would illegally revive that source.
        resolution = "target"
    if resolution == "target":
        _complete_transition_target(spec, effective)
    else:
        try:
            _restore_transition_source(spec, effective)
        except _RecoverySourceSchemaIncompatible:
            effective = _ensure_activation_schema_ready(spec, effective)
            _complete_transition_target(spec, effective)
            resolution = "target"
    return resolution


def _recover_pending_transition(
    spec: CloudDeploymentSpec,
) -> Mapping[str, Any] | None:
    journal = _transition_journal()
    if journal is None:
        return None
    try:
        resolution = _resolve_pending_transition(spec, journal)
    except (CloudDeployError, OSError):
        raise CloudDeployError("activation_recovery_failed") from None
    return {**journal, "resolution": resolution}


def _compensate_transition(
    spec: CloudDeploymentSpec, journal: Mapping[str, Any]
) -> str:
    try:
        return _resolve_pending_transition(spec, journal)
    except (CloudDeployError, OSError):
        raise CloudDeployError("activation_compensation_failed") from None


def deploy(spec: CloudDeploymentSpec, *, confirmation: str) -> Mapping[str, Any]:
    """Apply one local deployment after every immutable/target fence passes."""

    spec.validate()
    _target_preflight(spec, confirmation)
    _validate_attestation(spec)
    with _deployment_lock():
        recovered = _recover_pending_transition(spec)
        if (
            recovered is not None
            and recovered.get("operation") == "activate"
            and recovered.get("resolution") == "target"
            and isinstance(recovered.get("target_state"), Mapping)
            and recovered["target_state"].get("active_release_id") == spec.release_id
            and recovered["target_state"].get("artifact_manifest_sha256")
            == spec.artifact_manifest_sha256
        ):
            return dict(recovered["target_state"])
        manifest = _validate_artifact(spec)
        prior = _state()
        _validate_legacy_migration_plan(spec, prior)
        current = None if prior is None else str(prior["active_slot"])
        target = "blue" if current in {None, "green"} else "green"
        release = _install_release(spec, manifest)
        _install_deployment_templates(spec, release)
        _write_slot_environment(target, release)
        _verify_staged_runtime(release)
        _verify_nginx_wiring(spec)
        receipt = _activation_state(
            release_id=spec.release_id,
            slot=target,
            prior=prior,
            artifact_manifest_sha256=spec.artifact_manifest_sha256,
        )
        journal = _new_transition_journal(
            operation="activate",
            source_state=prior,
            target_state=receipt,
            legacy_admin_migration=(
                _legacy_migration_seed(spec) if prior is None else None
            ),
        )
        # The migration publishes ``migrating`` before either writer set is
        # stopped.  It remains inside the compensation boundary so a
        # deterministic failure can restore the schema-compatible source (or
        # the immutable first-release legacy source) instead of unnecessarily
        # extending an outage.  A process crash still leaves the durable
        # journal for idempotent startup recovery.
        try:
            journal = _ensure_activation_schema_ready(
                spec, journal, target_release=release
            )
            _start_target_services(spec, receipt)
            journal = _advance_transition_journal(journal, "target_ready")
            _switch_nginx(spec, target)
            journal = _advance_transition_journal(journal, "routes_switched")
            _atomic_write(
                STATE_ROOT / "active.json",
                _canonical_json(receipt) + b"\n",
                mode=0o600,
            )
            _fsync_directory(STATE_ROOT)
            journal = _advance_transition_journal(journal, "state_written")
            _atomic_symlink(release, INSTALL_ROOT / "current")
            _fsync_directory(INSTALL_ROOT)
            # Removing the durable journal is the only commit point. A crash
            # before this unlink remains incomplete. Startup chooses from live
            # routes, then schema-checks every service role before starting a
            # source; an incompatible or unverifiable source rolls forward.
            _clear_transition_journal()
        except (CloudDeployError, OSError) as error:
            # Always compensate from the newest durable authority. The local
            # variable may predate a receipt written inside the migration
            # helper immediately before a process/database failure.
            durable = _transition_journal() or journal
            resolution = _compensate_transition(spec, durable)
            if resolution == "target":
                return receipt
            if isinstance(error, CloudDeployError):
                raise
            raise CloudDeployError("activation_commit_failed") from None
        return receipt


def rollback(spec: CloudDeploymentSpec, *, confirmation: str) -> Mapping[str, Any]:
    """Return traffic to the recorded known-good slot without schema downgrade."""

    spec.validate()
    _target_preflight(spec, confirmation)
    _validate_attestation(spec)
    with _deployment_lock():
        recovered = _recover_pending_transition(spec)
        if (
            recovered is not None
            and recovered.get("operation") == "rollback"
            and recovered.get("resolution") == "target"
        ):
            target = recovered.get("target_state")
            if isinstance(target, Mapping):
                return dict(target)
            source = recovered.get("source_state")
            if isinstance(source, Mapping):
                return _legacy_rollback_receipt(source)
            raise CloudDeployError("activation_recovery_failed")
        current = _state()
        if current is None:
            raise CloudDeployError("rollback_state_missing")
        previous_target_type = current.get("previous_target_type")
        previous_slot = current.get("previous_slot")
        previous_release = current.get("previous_release_id")
        if previous_target_type == "legacy":
            target_state = None
            receipt = _legacy_rollback_receipt(current)
            previous_root = None
        elif previous_target_type == "slot" and previous_slot in SLOTS and SAFE_RELEASE_ID.fullmatch(
            str(previous_release or "")
        ):
            previous_root = _release_for_state(
                {"active_release_id": str(previous_release)}
            )
            previous_manifest_sha256 = _sha256_file(
                previous_root / "cloud-release-manifest.json"
            )
            target_state = _rollback_slot_state(
                current,
                release_id=str(previous_release),
                slot=str(previous_slot),
                artifact_manifest_sha256=previous_manifest_sha256,
            )
            previous_root = _verify_transition_release(spec, target_state)
            receipt = target_state
        else:
            raise CloudDeployError("rollback_target_missing")
        active_slot = str(current["active_slot"])
        journal = _new_transition_journal(
            operation="rollback", source_state=current, target_state=target_state
        )
        try:
            _systemctl(spec, "stop", reversed(_slot_units(active_slot)))
            if target_state is None:
                _start_target_services(spec, None)
                _switch_nginx_legacy(spec)
            else:
                assert previous_root is not None
                # Old code validates the already-migrated schema. It is never
                # allowed to downgrade or restore a live database automatically.
                for service in _SERVICE_MODULES:
                    _run_service_command(
                        _service_command(previous_root, service, "schema", "check"),
                        code="rollback_schema_incompatible",
                        environment=_service_environment(service, str(previous_slot)),
                        timeout=600,
                    )
                _start_target_services(spec, target_state)
                journal = _advance_transition_journal(journal, "target_ready")
                _switch_nginx(spec, str(previous_slot))
            journal = _advance_transition_journal(journal, "routes_switched")
            if target_state is None:
                _remove_slot_projection()
            else:
                _atomic_write(
                    STATE_ROOT / "active.json",
                    _canonical_json(target_state) + b"\n",
                    mode=0o600,
                )
                _fsync_directory(STATE_ROOT)
                journal = _advance_transition_journal(journal, "state_written")
                _atomic_symlink(previous_root, INSTALL_ROOT / "current")
                _fsync_directory(INSTALL_ROOT)
            # Journal removal, not an intermediate phase label, commits the
            # rollback. Until then startup resolves live routes and schema
            # compatibility; it never blindly restores the source target.
            _clear_transition_journal()
        except (CloudDeployError, OSError) as error:
            resolution = _compensate_transition(spec, journal)
            if resolution == "target":
                return receipt
            if isinstance(error, CloudDeployError):
                raise
            raise CloudDeployError("rollback_commit_failed") from None
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
