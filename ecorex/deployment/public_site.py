"""Root-only atomic deployment authority for the public v1 Web site.

The public site is deliberately deployed independently from the cloud service
slot transaction.  This module accepts only the fixed server-local staging
layout, turns it into one immutable release-id slot, and atomically rebinds the
``current`` directory pointer.  It has no SSH, upload, build or signing
capability.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from ecorex import __version__
from ecorex.control_plane.admin_web import AdminWebAssetError, AdminWebAssets
from ecorex.deployment.cloud_artifact import cloud_manifest_signing_payload
from ecorex.release import validate_public_bootstrap_index
from ecorex.release.evidence_io import (
    read_stable_regular_file,
    strict_json_loads,
)
from ecorex.release.process_boundary import run_bounded_process
from ecorex.update import Ed25519SignatureVerifier, SignatureVerifier

try:  # The read-only planner remains importable on Windows release workers.
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
PUBLIC_ORIGIN = "https://dl.ecoremedia.net/ecorex-agent/"
PUBLIC_HOST = "dl.ecoremedia.net"
ADMIN_URL = f"{PUBLIC_ORIGIN}admin/"
ADMIN_HEALTH_URL = f"{PUBLIC_ORIGIN}admin/health/ready"
ADMIN_EXTERNAL_ASSET_PREFIX = "/ecorex-agent/admin/assets"
ADMIN_VERSION_HEADER = "x-ecorex-product-version"
ADMIN_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "manifest-src 'none'",
        "worker-src 'none'",
    )
)

DOWNLOAD_ROOT = Path("/srv/ecorex-agent-download")
# Existing production download ownership is the dedicated static-publication
# account (uid 994), while a clean install may create the root as uid 0.
STATIC_READ_GID = 994
STAGING_ROOT = DOWNLOAD_ROOT / "site-staging"
SLOTS_ROOT = DOWNLOAD_ROOT / "site-slots"
LEGACY_ROOT = DOWNLOAD_ROOT / "legacy-sites"
CURRENT_PATH = DOWNLOAD_ROOT / "current"
PUBLIC_POINTER_PATH = (
    DOWNLOAD_ROOT / "public-pointer" / "public-bootstrap-index.json"
)
STATE_ROOT = Path("/var/lib/ecorex/site-deploy")
RECEIPT_ROOT = STATE_ROOT / "receipts"
JOURNAL_PATH = STATE_ROOT / "activation-pending.json"
# Share the production deploy lock with the cloud sidecar.  The public admin
# readback depends on that route, so the two activation authorities must never
# mutate concurrently.
LOCK_PATH = Path("/run/lock/ecorex-cloud-deploy.lock")

NGINX_BINARY = Path("/usr/sbin/nginx")
SYSTEMCTL_BINARY = Path("/usr/bin/systemctl")
CURL_BINARY = Path("/usr/bin/curl")
RELEASE_KEYRING_PATH = Path("/etc/ecorex/cloud/release-public-keys.json")
PUBLICATION_KEYRING_PATH = Path(
    "/etc/ecorex/cloud/publication-public-keys.json"
)

PUBLIC_SITE_AUTHORIZATION_DOMAIN = b"ecorex.public-site-deployment.v1\0"
PUBLIC_SITE_AUTHORIZATION_TYPE = "ecorex.public-site-deployment-authorization"

SAFE_RELEASE_ID = re.compile(r"release-stable-[0-9a-f]{24}\Z")
SAFE_HASHED_ASSET = re.compile(
    r"(?P<stem>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\."
    r"(?P<digest>[0-9a-f]{12})\."
    r"(?P<suffix>js|css|png|svg|webp|woff2)\Z"
)
SAFE_VERSION = re.compile(
    r"1\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\Z"
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TRANSACTION_ID = re.compile(r"site-[0-9a-f]{32}\Z")

MAX_HTML_BYTES = 256 * 1024
MAX_POINTER_BYTES = 256 * 1024
MAX_CODE_ASSET_BYTES = 2 * 1024 * 1024
MAX_MEDIA_ASSET_BYTES = 8 * 1024 * 1024
MAX_ADMIN_READBACK_BYTES = 2 * 1024 * 1024
MAX_SITE_BYTES = 32 * 1024 * 1024
MAX_ASSET_COUNT = 32

_DIRECT_RECEIPT_KEYS = frozenset(
    {
        "ok",
        "status",
        "protected_pipeline_passed",
        "release_id",
        "version",
        "manifest_sha256",
        "waiver_sha256",
        "publication_receipt_sha256",
        "public_index_sha256",
        "public_index_status",
    }
)
_JOURNAL_PHASES = frozenset({"prepared", "current_switched", "verified"})
_SOURCE_KINDS = frozenset({"absent", "symlink", "legacy_directory"})


class PublicSiteDeployError(RuntimeError):
    """Redacted operator-safe failure."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,95}", code) is None:
            code = "public_site_deployment_failed"
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class DeploymentPaths:
    download_root: Path = DOWNLOAD_ROOT
    staging_root: Path = STAGING_ROOT
    slots_root: Path = SLOTS_ROOT
    legacy_root: Path = LEGACY_ROOT
    current_path: Path = CURRENT_PATH
    public_pointer_path: Path = PUBLIC_POINTER_PATH
    state_root: Path = STATE_ROOT
    receipt_root: Path = RECEIPT_ROOT
    journal_path: Path = JOURNAL_PATH
    lock_path: Path = LOCK_PATH
    release_keyring_path: Path = RELEASE_KEYRING_PATH
    publication_keyring_path: Path = PUBLICATION_KEYRING_PATH

    @classmethod
    def for_test(cls, root: Path) -> "DeploymentPaths":
        download = root / "download"
        state = root / "state"
        return cls(
            download_root=download,
            staging_root=download / "site-staging",
            slots_root=download / "site-slots",
            legacy_root=download / "legacy-sites",
            current_path=download / "current",
            public_pointer_path=(
                download / "public-pointer" / "public-bootstrap-index.json"
            ),
            state_root=state,
            receipt_root=state / "receipts",
            journal_path=state / "activation-pending.json",
            lock_path=root / "run" / "ecorex-cloud-deploy.lock",
            release_keyring_path=root / "config" / "release-public-keys.json",
            publication_keyring_path=(
                root / "config" / "publication-public-keys.json"
            ),
        )

    @classmethod
    def for_offline_staging(cls, release_stage: Path) -> "DeploymentPaths":
        """Fence workstation signing to one self-contained staging parent.

        This constructor is intentionally distinct from the production
        defaults.  It supplies no usable production slot/current/state paths;
        the unsigned validator only reads ``download_root``, ``staging_root``
        and the exact release directory.  ``apply`` still rejects this object
        through its exact ``DeploymentPaths()`` production fence.
        """

        try:
            release = release_stage.expanduser().resolve(strict=True)
            root = release.parent.resolve(strict=True)
        except OSError:
            raise PublicSiteDeployError("site_authorization_staging_invalid") from None
        if release.parent != root or not release.is_dir():
            raise PublicSiteDeployError("site_authorization_staging_invalid")
        unused = root / ".offline-signing-no-deploy"
        return cls(
            download_root=root,
            staging_root=root,
            slots_root=unused / "site-slots",
            legacy_root=unused / "legacy-sites",
            current_path=unused / "current",
            public_pointer_path=release / "site" / "public-bootstrap-index.json",
            state_root=unused / "state",
            receipt_root=unused / "receipts",
            journal_path=unused / "activation-pending.json",
            lock_path=unused / "deploy.lock",
            release_keyring_path=unused / "release-public-keys.json",
            publication_keyring_path=unused / "publication-public-keys.json",
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SiteFile:
    relative_path: str
    payload: bytes
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


@dataclasses.dataclass(frozen=True, slots=True)
class ValidatedSite:
    root: Path
    release_id: str
    version: str
    build_digest: str
    manifest_sha256: str
    publication_receipt_sha256: str
    public_index_sha256: str
    waiver_sha256: str
    tree_sha256: str
    files: tuple[SiteFile, ...]
    public_index: SiteFile
    direct_receipt_sha256: str
    authorization_sha256: str | None
    admin_identity: Mapping[str, Any] | None

    def file(self, relative_path: str) -> SiteFile:
        if relative_path == self.public_index.relative_path:
            return self.public_index
        for value in self.files:
            if value.relative_path == relative_path:
                return value
        raise PublicSiteDeployError("site_file_missing")


@dataclasses.dataclass(frozen=True, slots=True)
class PublicPointerTrust:
    authority_verifier: SignatureVerifier
    freshness_verifier: SignatureVerifier


@dataclasses.dataclass(frozen=True, slots=True)
class HttpReadback:
    status: int
    headers: Mapping[str, tuple[str, ...]]
    body: bytes

    def values(self, name: str) -> tuple[str, ...]:
        return self.headers.get(name.casefold(), ())


class ReadbackClient(Protocol):
    def get(self, url: str, *, maximum_bytes: int) -> HttpReadback:
        ...


class ServerController(Protocol):
    def validate(self) -> None:
        ...

    def reload(self) -> None:
        ...


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise PublicSiteDeployError("deployment_document_invalid") from None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # Windows only exercises the read-only/unit surface.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise PublicSiteDeployError("deployment_directory_sync_failed") from None


def _linked(value: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse
    )


def _require_plain_directory(
    path: Path,
    *,
    code: str,
    expected_owner_uid: int | None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise PublicSiteDeployError(code) from None
    if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicSiteDeployError(code)
    if expected_owner_uid is not None and (
        metadata.st_uid != expected_owner_uid or metadata.st_mode & 0o022
    ):
        raise PublicSiteDeployError(code)
    return metadata


def _require_secure_path(
    path: Path,
    *,
    code: str,
    expected_owner_uid: int | None,
) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    # Host roots (/srv, /var) are system managed and may legitimately be 0755;
    # the configured EcoreX root and all descendants are checked separately.
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            raise PublicSiteDeployError(code) from None
        if _linked(metadata):
            raise PublicSiteDeployError(code)
    _require_plain_directory(
        absolute,
        code=code,
        expected_owner_uid=expected_owner_uid,
    )


def _read_site_file(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    try:
        return read_stable_regular_file(
            path,
            maximum_bytes=maximum_bytes,
            code=code,
        )
    except ValueError:
        raise PublicSiteDeployError(code) from None


def _require_secure_regular_file(
    path: Path,
    *,
    code: str,
    expected_owner_uid: int | None,
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise PublicSiteDeployError(code) from None
    if (
        _linked(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_nlink", 1) != 1
    ):
        raise PublicSiteDeployError(code)
    if expected_owner_uid is not None and (
        metadata.st_uid != expected_owner_uid or metadata.st_mode & 0o022
    ):
        raise PublicSiteDeployError(code)


def _relative_references(html: str) -> frozenset[str]:
    references = re.findall(r'(?:src|href)="\./([^"#?]+)', html)
    result: set[str] = set()
    for value in references:
        normalized = PurePosixPath(value)
        if (
            normalized.is_absolute()
            or not normalized.parts
            or ".." in normalized.parts
            or "." in normalized.parts
        ):
            raise PublicSiteDeployError("site_html_reference_invalid")
        result.add(normalized.as_posix())
    return frozenset(result)


def _validate_direct_receipt(
    path: Path,
    *,
    expected_release_id: str,
    expected_owner_uid: int | None,
) -> tuple[dict[str, Any], bytes]:
    payload = _read_site_file(
        path,
        maximum_bytes=64 * 1024,
        code="direct_deployable_receipt_invalid",
    )
    try:
        parsed = strict_json_loads(
            payload,
            code="direct_deployable_receipt_invalid",
        )
    except ValueError:
        raise PublicSiteDeployError("direct_deployable_receipt_invalid") from None
    try:
        metadata = path.lstat()
    except OSError:
        raise PublicSiteDeployError("direct_deployable_receipt_invalid") from None
    if expected_owner_uid is not None and (
        metadata.st_uid != expected_owner_uid or metadata.st_mode & 0o022
    ):
        raise PublicSiteDeployError("direct_deployable_receipt_not_root_owned")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != _DIRECT_RECEIPT_KEYS
        or parsed.get("ok") is not True
        or parsed.get("status") != "deployable-with-explicit-operator-waiver"
        or parsed.get("protected_pipeline_passed") is not False
        or parsed.get("release_id") != expected_release_id
        or parsed.get("public_index_status") != "published"
        or SAFE_VERSION.fullmatch(str(parsed.get("version") or "")) is None
        or any(
            SHA256.fullmatch(str(parsed.get(name) or "")) is None
            for name in (
                "manifest_sha256",
                "waiver_sha256",
                "publication_receipt_sha256",
                "public_index_sha256",
            )
        )
    ):
        raise PublicSiteDeployError("direct_deployable_receipt_invalid")
    if payload != _canonical_json(parsed) + b"\n":
        raise PublicSiteDeployError("direct_deployable_receipt_not_canonical")
    return parsed, payload


def _validate_unsigned_staged_site(
    release_id: str,
    *,
    paths: DeploymentPaths = DeploymentPaths(),
    expected_owner_uid: int | None = 0,
    authorization_expected: bool,
) -> ValidatedSite:
    """Validate and bind site bytes before trusting any authorization."""

    if SAFE_RELEASE_ID.fullmatch(release_id) is None:
        raise PublicSiteDeployError("release_id_invalid")
    download_metadata = _require_plain_directory(
        paths.download_root,
        code="product_download_root_invalid",
        expected_owner_uid=None,
    )
    if expected_owner_uid is not None and (
        download_metadata.st_uid != 0
        or download_metadata.st_gid != STATIC_READ_GID
        or stat.S_IMODE(download_metadata.st_mode) != 0o755
    ):
        raise PublicSiteDeployError("product_download_root_invalid")
    release_stage = paths.staging_root / release_id
    site_root = release_stage / "site"
    receipt_path = release_stage / "direct-deployable.json"
    _require_secure_path(
        release_stage,
        code="site_staging_invalid",
        expected_owner_uid=expected_owner_uid,
    )
    try:
        stage_entries = {entry.name for entry in release_stage.iterdir()}
    except OSError:
        raise PublicSiteDeployError("site_staging_invalid") from None
    expected_stage_entries = {"site", "direct-deployable.json"}
    if authorization_expected:
        expected_stage_entries.add("deployment-authorization.json")
    if stage_entries != expected_stage_entries:
        raise PublicSiteDeployError("site_staging_contains_unexpected_entries")
    _require_secure_path(
        site_root,
        code="site_staging_invalid",
        expected_owner_uid=expected_owner_uid,
    )
    if expected_owner_uid is not None:
        device = download_metadata.st_dev
        for directory in (release_stage, site_root):
            metadata = directory.lstat()
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_dev != device
            ):
                raise PublicSiteDeployError("production_staging_layout_invalid")
    receipt, receipt_payload = _validate_direct_receipt(
        receipt_path,
        expected_release_id=release_id,
        expected_owner_uid=expected_owner_uid,
    )
    if expected_owner_uid is not None:
        receipt_metadata = receipt_path.lstat()
        if (
            receipt_metadata.st_gid != 0
            or stat.S_IMODE(receipt_metadata.st_mode) != 0o600
            or receipt_metadata.st_dev != download_metadata.st_dev
        ):
            raise PublicSiteDeployError("production_staging_layout_invalid")

    try:
        root_entries = {entry.name: entry for entry in site_root.iterdir()}
    except OSError:
        raise PublicSiteDeployError("site_staging_invalid") from None
    allowed_directories = {"assets"}
    directories: set[str] = set()
    root_files: dict[str, Path] = {}
    for name, entry in root_entries.items():
        try:
            metadata = entry.lstat()
        except OSError:
            raise PublicSiteDeployError("site_staging_changed") from None
        if _linked(metadata):
            raise PublicSiteDeployError("site_staging_link_forbidden")
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(name)
        elif stat.S_ISREG(metadata.st_mode):
            root_files[name] = entry
        else:
            raise PublicSiteDeployError("site_staging_entry_invalid")
    if directories != allowed_directories:
        raise PublicSiteDeployError("site_file_set_invalid")

    script_names = sorted(name for name in root_files if re.fullmatch(r"site\.[0-9a-f]{12}\.js", name))
    style_names = sorted(name for name in root_files if re.fullmatch(r"styles\.[0-9a-f]{12}\.css", name))
    expected_root_files = {
        "index.html",
        "public-bootstrap-index.json",
        *script_names,
        *style_names,
    }
    if (
        len(script_names) != 1
        or len(style_names) != 1
        or set(root_files) != expected_root_files
    ):
        raise PublicSiteDeployError("site_file_set_invalid")

    assets_root = site_root / "assets"
    _require_plain_directory(
        assets_root,
        code="site_assets_invalid",
        expected_owner_uid=expected_owner_uid,
    )
    if expected_owner_uid is not None:
        assets_metadata = assets_root.lstat()
        if (
            assets_metadata.st_uid != 0
            or assets_metadata.st_gid != 0
            or stat.S_IMODE(assets_metadata.st_mode) != 0o700
            or assets_metadata.st_dev != download_metadata.st_dev
        ):
            raise PublicSiteDeployError("production_staging_layout_invalid")
    try:
        asset_entries = sorted(assets_root.iterdir(), key=lambda value: value.name)
    except OSError:
        raise PublicSiteDeployError("site_assets_invalid") from None
    if not 1 <= len(asset_entries) <= MAX_ASSET_COUNT:
        raise PublicSiteDeployError("site_asset_count_invalid")

    raw_files: list[tuple[str, Path, int]] = [
        ("index.html", root_files["index.html"], MAX_HTML_BYTES),
        (
            "public-bootstrap-index.json",
            root_files["public-bootstrap-index.json"],
            MAX_POINTER_BYTES,
        ),
        (script_names[0], root_files[script_names[0]], MAX_CODE_ASSET_BYTES),
        (style_names[0], root_files[style_names[0]], MAX_CODE_ASSET_BYTES),
    ]
    for entry in asset_entries:
        try:
            metadata = entry.lstat()
        except OSError:
            raise PublicSiteDeployError("site_staging_changed") from None
        match = SAFE_HASHED_ASSET.fullmatch(entry.name)
        if _linked(metadata) or not stat.S_ISREG(metadata.st_mode) or match is None:
            raise PublicSiteDeployError("site_asset_invalid")
        raw_files.append(
            (f"assets/{entry.name}", entry, MAX_MEDIA_ASSET_BYTES)
        )

    files: list[SiteFile] = []
    total = 0
    for relative, path, maximum in raw_files:
        _require_secure_regular_file(
            path,
            code="site_file_not_root_owned",
            expected_owner_uid=expected_owner_uid,
        )
        if expected_owner_uid is not None:
            metadata = path.lstat()
            if (
                metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != download_metadata.st_dev
            ):
                raise PublicSiteDeployError("production_staging_layout_invalid")
        payload = _read_site_file(path, maximum_bytes=maximum, code="site_file_invalid")
        digest = _sha256(payload)
        name = PurePosixPath(relative).name
        if name not in {"index.html", "public-bootstrap-index.json"}:
            match = SAFE_HASHED_ASSET.fullmatch(name)
            if match is None or not digest.startswith(match.group("digest")):
                raise PublicSiteDeployError("site_content_address_invalid")
        total += len(payload)
        if total > MAX_SITE_BYTES:
            raise PublicSiteDeployError("site_size_limit_exceeded")
        files.append(SiteFile(relative, payload, digest))

    html_file = next(value for value in files if value.relative_path == "index.html")
    try:
        html = html_file.payload.decode("utf-8")
    except UnicodeDecodeError:
        raise PublicSiteDeployError("site_html_invalid") from None
    references = _relative_references(html)
    expected_references = frozenset(
        value.relative_path
        for value in files
        if value.relative_path != "index.html"
        and value.relative_path != "public-bootstrap-index.json"
    )
    if (
        references != expected_references
        or 'href="/ecorex-agent/admin/"' not in html
        or 'href="/admin/"' in html
    ):
        raise PublicSiteDeployError("site_html_reference_invalid")
    script = next(value for value in files if value.relative_path == script_names[0])
    if b'fetch("./public-bootstrap-index.json"' not in script.payload:
        raise PublicSiteDeployError("site_pointer_reference_missing")

    pointer_file = next(
        value
        for value in files
        if value.relative_path == "public-bootstrap-index.json"
    )
    try:
        pointer = strict_json_loads(
            pointer_file.payload,
            code="site_public_index_invalid",
        )
        if not isinstance(pointer, dict):
            raise ValueError("site_public_index_invalid")
        validate_public_bootstrap_index(pointer)
    except (ValueError, TypeError):
        raise PublicSiteDeployError("site_public_index_invalid") from None
    release = pointer.get("release")
    authority = pointer.get("authority")
    target = authority.get("target") if isinstance(authority, dict) else None
    if (
        pointer.get("status") != "published"
        or not isinstance(release, dict)
        or not isinstance(target, dict)
        or release.get("release_id") != release_id
        or release.get("release_id") != target.get("release_id")
        or release.get("version") != receipt["version"]
        or release.get("version") != target.get("version")
        or release.get("build_digest") != target.get("build_digest")
        or release.get("publication_receipt_sha256")
        != receipt["publication_receipt_sha256"]
        or target.get("manifest_sha256") != receipt["manifest_sha256"]
        or pointer_file.sha256 != receipt["public_index_sha256"]
    ):
        raise PublicSiteDeployError("site_published_identity_mismatch")

    ordered = tuple(
        sorted(
            (
                value
                for value in files
                if value.relative_path != "public-bootstrap-index.json"
            ),
            key=lambda value: value.relative_path,
        )
    )
    # Re-list after all stable reads.  A root-side staging mutation cannot add
    # an unbound file between the initial exact-set check and deployment.
    if {entry.name for entry in site_root.iterdir()} != set(root_entries) or {
        entry.name for entry in assets_root.iterdir()
    } != {entry.name for entry in asset_entries}:
        raise PublicSiteDeployError("site_staging_changed")
    tree = [
        {
            "path": value.relative_path,
            "sha256": value.sha256,
            "size_bytes": value.size_bytes,
        }
        for value in ordered
    ]
    return ValidatedSite(
        root=site_root,
        release_id=release_id,
        version=str(release["version"]),
        build_digest=str(release["build_digest"]),
        manifest_sha256=str(receipt["manifest_sha256"]),
        publication_receipt_sha256=str(receipt["publication_receipt_sha256"]),
        public_index_sha256=pointer_file.sha256,
        waiver_sha256=str(receipt["waiver_sha256"]),
        tree_sha256=_sha256(_canonical_json(tree)),
        files=ordered,
        public_index=pointer_file,
        direct_receipt_sha256=_sha256(receipt_payload),
        authorization_sha256=None,
        admin_identity=None,
    )


def validate_admin_deployment_identity(
    value: Mapping[str, Any],
    *,
    version: str,
) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema_version",
            "cloud_release_id",
            "cloud_version",
            "cloud_manifest_sha256",
            "cloud_manifest_key_id",
            "index",
            "assets",
            "version_marker",
            "health",
        }
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("cloud_release_id"), str)
        or not str(value["cloud_release_id"]).startswith("ecorex-cloud-v")
        or value.get("cloud_version") != version
        or SHA256.fullmatch(str(value.get("cloud_manifest_sha256") or "")) is None
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            str(value.get("cloud_manifest_key_id") or ""),
        )
        is None
    ):
        raise PublicSiteDeployError("admin_deployment_identity_invalid")
    index = value.get("index")
    if (
        not isinstance(index, Mapping)
        or set(index) != {"path", "sha256", "size_bytes"}
        or index.get("path") != "/ecorex-agent/admin/"
        or SHA256.fullmatch(str(index.get("sha256") or "")) is None
        or isinstance(index.get("size_bytes"), bool)
        or not isinstance(index.get("size_bytes"), int)
        or not 1 <= int(index["size_bytes"]) <= MAX_ADMIN_READBACK_BYTES
    ):
        raise PublicSiteDeployError("admin_deployment_identity_invalid")
    assets = value.get("assets")
    if not isinstance(assets, list) or len(assets) != 2:
        raise PublicSiteDeployError("admin_deployment_identity_invalid")
    observed_paths: list[str] = []
    for asset in assets:
        if (
            not isinstance(asset, Mapping)
            or set(asset) != {"path", "sha256", "size_bytes", "media_type"}
            or not isinstance(asset.get("path"), str)
            or not str(asset["path"]).startswith(
                f"{ADMIN_EXTERNAL_ASSET_PREFIX}/admin."
            )
            or not str(asset["path"]).endswith((".css", ".js"))
            or SHA256.fullmatch(str(asset.get("sha256") or "")) is None
            or f".{asset['sha256']}." not in str(asset["path"])
            or isinstance(asset.get("size_bytes"), bool)
            or not isinstance(asset.get("size_bytes"), int)
            or not 1 <= int(asset["size_bytes"]) <= MAX_ADMIN_READBACK_BYTES
            or asset.get("media_type") not in {"text/css", "text/javascript"}
        ):
            raise PublicSiteDeployError("admin_deployment_identity_invalid")
        observed_paths.append(str(asset["path"]))
    if len(set(observed_paths)) != 2 or not {
        Path(path).suffix for path in observed_paths
    } == {".css", ".js"}:
        raise PublicSiteDeployError("admin_deployment_identity_invalid")
    version_marker = value.get("version_marker")
    if version_marker != {
        "header": "X-EcoreX-Product-Version",
        "value": version,
    }:
        raise PublicSiteDeployError("admin_deployment_identity_invalid")
    health = value.get("health")
    if (
        not isinstance(health, Mapping)
        or set(health) != {"path", "status", "body_sha256", "body_size_bytes"}
        or health.get("path") != "/ecorex-agent/admin/health/ready"
        or health.get("status") != 200
        or health.get("body_sha256") != _sha256(b'{"status":"ready"}')
        or health.get("body_size_bytes") != len(b'{"status":"ready"}')
    ):
        raise PublicSiteDeployError("admin_deployment_identity_invalid")


def build_admin_deployment_identity(
    cloud_artifact_root: Path,
    *,
    public_keys: Mapping[str, bytes],
) -> dict[str, Any]:
    """Verify a signed cloud artifact and project its exact Admin Web bytes."""

    try:
        root = cloud_artifact_root.expanduser().resolve(strict=True)
        metadata = root.lstat()
    except OSError:
        raise PublicSiteDeployError("admin_cloud_artifact_invalid") from None
    if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicSiteDeployError("admin_cloud_artifact_invalid")
    manifest_bytes = _read_site_file(
        root / "cloud-release-manifest.json",
        maximum_bytes=16 * 1024 * 1024,
        code="admin_cloud_manifest_invalid",
    )
    signature_bytes = _read_site_file(
        root / "cloud-release-manifest.sig.json",
        maximum_bytes=64 * 1024,
        code="admin_cloud_manifest_invalid",
    )
    try:
        manifest = strict_json_loads(manifest_bytes, code="admin_cloud_manifest_invalid")
        signature = strict_json_loads(
            signature_bytes, code="admin_cloud_manifest_invalid"
        )
    except ValueError:
        raise PublicSiteDeployError("admin_cloud_manifest_invalid") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
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
        or manifest.get("schema_version") != 1
        or manifest.get("version") != __version__
        or manifest.get("platform") != "linux"
        or manifest.get("architecture") != "aarch64"
        or not isinstance(manifest.get("files"), list)
        or not isinstance(signature, dict)
        or set(signature) != {"key_id", "manifest_sha256", "signature_b64"}
        or signature.get("manifest_sha256") != _sha256(manifest_bytes)
        or not isinstance(signature.get("key_id"), str)
    ):
        raise PublicSiteDeployError("admin_cloud_manifest_invalid")
    public = public_keys.get(str(signature["key_id"]))
    try:
        raw_signature = base64.b64decode(
            str(signature.get("signature_b64") or ""), validate=True
        )
        if public is None or len(public) != 32 or len(raw_signature) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(
            raw_signature,
            cloud_manifest_signing_payload(manifest),
        )
    except (InvalidSignature, TypeError, ValueError):
        raise PublicSiteDeployError("admin_cloud_signature_rejected") from None

    suffixes = {
        name: f"ecorex/control_plane/admin_web/static/{name}"
        for name in ("index.html", "admin.css", "admin.js", "asset-manifest.json")
    }
    rows: dict[str, Mapping[str, Any]] = {}
    for name, suffix in suffixes.items():
        matches = [
            row
            for row in manifest["files"]
            if isinstance(row, Mapping)
            and isinstance(row.get("path"), str)
            and str(row["path"]).endswith(suffix)
        ]
        if len(matches) != 1:
            raise PublicSiteDeployError("admin_cloud_assets_missing")
        rows[name] = matches[0]
    parents = {
        (root / str(row["path"])).parent.resolve(strict=True)
        for row in rows.values()
    }
    if len(parents) != 1:
        raise PublicSiteDeployError("admin_cloud_assets_invalid")
    static_root = parents.pop()
    for name, row in rows.items():
        path = root / str(row["path"])
        payload = _read_site_file(
            path,
            maximum_bytes=4 * 1024 * 1024,
            code="admin_cloud_assets_invalid",
        )
        if (
            row.get("sha256") != _sha256(payload)
            or row.get("size_bytes") != len(payload)
        ):
            raise PublicSiteDeployError("admin_cloud_assets_invalid")
    try:
        assets = AdminWebAssets.load(static_root)
        rendered_index = assets.render_index(ADMIN_EXTERNAL_ASSET_PREFIX).encode(
            "utf-8"
        )
    except (AdminWebAssetError, UnicodeError):
        raise PublicSiteDeployError("admin_cloud_assets_invalid") from None
    projected_assets = [
        {
            "path": f"{ADMIN_EXTERNAL_ASSET_PREFIX}/{asset.public_name}",
            "sha256": asset.digest,
            "size_bytes": len(asset.content),
            "media_type": asset.media_type,
        }
        for asset in sorted(assets.assets.values(), key=lambda item: item.public_name)
    ]
    value = {
        "schema_version": SCHEMA_VERSION,
        "cloud_release_id": str(manifest["release_id"]),
        "cloud_version": str(manifest["version"]),
        "cloud_manifest_sha256": _sha256(manifest_bytes),
        "cloud_manifest_key_id": str(signature["key_id"]),
        "index": {
            "path": "/ecorex-agent/admin/",
            "sha256": _sha256(rendered_index),
            "size_bytes": len(rendered_index),
        },
        "assets": projected_assets,
        "version_marker": {
            "header": "X-EcoreX-Product-Version",
            "value": str(manifest["version"]),
        },
        "health": {
            "path": "/ecorex-agent/admin/health/ready",
            "status": 200,
            "body_sha256": _sha256(b'{"status":"ready"}'),
            "body_size_bytes": len(b'{"status":"ready"}'),
        },
    }
    validate_admin_deployment_identity(value, version=str(manifest["version"]))
    return value


def public_site_authorization_payload(site: ValidatedSite) -> dict[str, Any]:
    """Return the exact identity authorized by the workstation release key."""

    if site.admin_identity is None:
        raise PublicSiteDeployError("admin_deployment_identity_missing")
    validate_admin_deployment_identity(site.admin_identity, version=site.version)
    return {
        "release_id": site.release_id,
        "version": site.version,
        "manifest_sha256": site.manifest_sha256,
        "waiver_sha256": site.waiver_sha256,
        "publication_receipt_sha256": site.publication_receipt_sha256,
        "public_index_sha256": site.public_index_sha256,
        "direct_receipt_sha256": site.direct_receipt_sha256,
        "site_tree_sha256": site.tree_sha256,
        "admin": dict(site.admin_identity),
    }


def public_site_authorization_signing_bytes(site: ValidatedSite) -> bytes:
    return PUBLIC_SITE_AUTHORIZATION_DOMAIN + _canonical_json(
        public_site_authorization_payload(site)
    )


def sign_public_site_authorization(site: ValidatedSite, *, signer: Any) -> dict[str, Any]:
    """Sign one fully rescanned site with the release (never freshness) key."""

    key_id = getattr(signer, "key_id", None)
    if not isinstance(key_id, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id
    ) is None:
        raise PublicSiteDeployError("site_authorization_signer_invalid")
    try:
        signature = signer.sign(public_site_authorization_signing_bytes(site))
    except Exception:
        raise PublicSiteDeployError("site_authorization_signing_failed") from None
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise PublicSiteDeployError("site_authorization_signature_invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": PUBLIC_SITE_AUTHORIZATION_TYPE,
        "authorization": public_site_authorization_payload(site),
        "signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }


def _read_public_keyring(
    path: Path,
    *,
    expected_owner_uid: int | None,
    code: str,
) -> dict[str, bytes]:
    _require_secure_regular_file(
        path,
        code=code,
        expected_owner_uid=expected_owner_uid,
    )
    payload = _read_site_file(
        path,
        maximum_bytes=64 * 1024,
        code=code,
    )
    try:
        value = strict_json_loads(payload, code=code)
    except ValueError:
        raise PublicSiteDeployError(code) from None
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise PublicSiteDeployError(code)
    keys: dict[str, bytes] = {}
    for key_id, encoded in value.items():
        if (
            not isinstance(key_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id) is None
            or not isinstance(encoded, str)
        ):
            raise PublicSiteDeployError(code)
        try:
            public = base64.b64decode(encoded, validate=True)
            Ed25519PublicKey.from_public_bytes(public)
        except (TypeError, ValueError):
            raise PublicSiteDeployError(code) from None
        if len(public) != 32:
            raise PublicSiteDeployError(code)
        keys[key_id] = public
    return keys


def _read_public_pointer_trust(
    paths: DeploymentPaths,
    *,
    expected_owner_uid: int | None,
) -> PublicPointerTrust:
    authority_keys = _read_public_keyring(
        paths.release_keyring_path,
        expected_owner_uid=expected_owner_uid,
        code="release_keyring_invalid",
    )
    freshness_keys = _read_public_keyring(
        paths.publication_keyring_path,
        expected_owner_uid=expected_owner_uid,
        code="publication_keyring_invalid",
    )
    if set(authority_keys).intersection(freshness_keys) or set(
        authority_keys.values()
    ).intersection(freshness_keys.values()):
        raise PublicSiteDeployError("public_pointer_trust_roles_overlap")
    try:
        return PublicPointerTrust(
            authority_verifier=Ed25519SignatureVerifier(authority_keys),
            freshness_verifier=Ed25519SignatureVerifier(freshness_keys),
        )
    except (TypeError, ValueError):
        raise PublicSiteDeployError("public_pointer_trust_invalid") from None


def verify_public_site_authorization(
    site: ValidatedSite,
    authorization: Mapping[str, Any],
    *,
    public_keys: Mapping[str, bytes],
) -> None:
    if (
        set(authorization)
        != {"schema_version", "document_type", "authorization", "signature"}
        or authorization.get("schema_version") != SCHEMA_VERSION
        or authorization.get("document_type") != PUBLIC_SITE_AUTHORIZATION_TYPE
        or authorization.get("authorization")
        != public_site_authorization_payload(site)
    ):
        raise PublicSiteDeployError("site_authorization_identity_mismatch")
    signature = authorization.get("signature")
    if (
        not isinstance(signature, Mapping)
        or set(signature) != {"algorithm", "key_id", "value"}
        or signature.get("algorithm") != "ed25519"
        or not isinstance(signature.get("key_id"), str)
        or not isinstance(signature.get("value"), str)
    ):
        raise PublicSiteDeployError("site_authorization_signature_invalid")
    public = public_keys.get(str(signature["key_id"]))
    try:
        raw_signature = base64.b64decode(str(signature["value"]), validate=True)
        if public is None or len(public) != 32 or len(raw_signature) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(
            raw_signature,
            public_site_authorization_signing_bytes(site),
        )
    except (InvalidSignature, TypeError, ValueError):
        raise PublicSiteDeployError("site_authorization_signature_rejected") from None


def validate_staged_site(
    release_id: str,
    *,
    paths: DeploymentPaths = DeploymentPaths(),
    expected_owner_uid: int | None = 0,
) -> ValidatedSite:
    """Rescan the exact site and verify its independent deployment authority."""

    site = _validate_unsigned_staged_site(
        release_id,
        paths=paths,
        expected_owner_uid=expected_owner_uid,
        authorization_expected=True,
    )
    authorization_path = (
        paths.staging_root / release_id / "deployment-authorization.json"
    )
    _require_secure_regular_file(
        authorization_path,
        code="site_authorization_invalid",
        expected_owner_uid=expected_owner_uid,
    )
    if expected_owner_uid is not None:
        authorization_metadata = authorization_path.lstat()
        if (
            authorization_metadata.st_gid != 0
            or stat.S_IMODE(authorization_metadata.st_mode) != 0o600
            or authorization_metadata.st_dev
            != paths.download_root.lstat().st_dev
        ):
            raise PublicSiteDeployError("production_staging_layout_invalid")
    payload = _read_site_file(
        authorization_path,
        maximum_bytes=64 * 1024,
        code="site_authorization_invalid",
    )
    try:
        authorization = strict_json_loads(payload, code="site_authorization_invalid")
    except ValueError:
        raise PublicSiteDeployError("site_authorization_invalid") from None
    if (
        not isinstance(authorization, dict)
        or payload != _canonical_json(authorization) + b"\n"
    ):
        raise PublicSiteDeployError("site_authorization_invalid")
    authorization_body = authorization.get("authorization")
    admin_identity = (
        authorization_body.get("admin")
        if isinstance(authorization_body, Mapping)
        else None
    )
    if not isinstance(admin_identity, Mapping):
        raise PublicSiteDeployError("admin_deployment_identity_missing")
    validate_admin_deployment_identity(admin_identity, version=site.version)
    site = dataclasses.replace(site, admin_identity=dict(admin_identity))
    keys = _read_public_keyring(
        paths.release_keyring_path,
        expected_owner_uid=expected_owner_uid,
        code="release_keyring_invalid",
    )
    verify_public_site_authorization(site, authorization, public_keys=keys)
    # Re-scan after authorization verification so a same-UID staging mutation
    # cannot replace site bytes after the signed tree was accepted.
    rescanned = _validate_unsigned_staged_site(
        release_id,
        paths=paths,
        expected_owner_uid=expected_owner_uid,
        authorization_expected=True,
    )
    if (
        rescanned.tree_sha256 != site.tree_sha256
        or rescanned.files != site.files
        or rescanned.public_index != site.public_index
    ):
        raise PublicSiteDeployError("site_changed_after_authorization")
    rescanned = dataclasses.replace(
        rescanned,
        admin_identity=dict(admin_identity),
    )
    verify_public_site_authorization(rescanned, authorization, public_keys=keys)
    return dataclasses.replace(
        rescanned,
        authorization_sha256=_sha256(payload),
    )


class FixedNginxController:
    """Use only reviewed absolute binaries and the system nginx service."""

    @staticmethod
    def _run(argv: Sequence[str], *, code: str, timeout: float) -> None:
        try:
            result = run_bounded_process(
                tuple(argv),
                payload=None,
                cwd=Path("/"),
                environment={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout_seconds=timeout,
                max_stdout_bytes=64 * 1024,
                max_stderr_bytes=64 * 1024,
            )
        except Exception:
            raise PublicSiteDeployError(code) from None
        if result.returncode != 0:
            raise PublicSiteDeployError(code)

    def validate(self) -> None:
        self._run((str(NGINX_BINARY), "-t"), code="nginx_validation_failed", timeout=20)

    def reload(self) -> None:
        self.validate()
        self._run(
            (str(SYSTEMCTL_BINARY), "reload", "nginx.service"),
            code="nginx_reload_failed",
            timeout=30,
        )
        self._run(
            (str(SYSTEMCTL_BINARY), "is-active", "--quiet", "nginx.service"),
            code="nginx_not_active",
            timeout=10,
        )


class FixedCurlReadback:
    """Read the public TLS virtual host through loopback and its real SNI."""

    def __init__(self, state_root: Path = STATE_ROOT) -> None:
        self._state_root = state_root

    def get(self, url: str, *, maximum_bytes: int) -> HttpReadback:
        if (
            not url.startswith(PUBLIC_ORIGIN)
            or maximum_bytes < 1
            or maximum_bytes > MAX_MEDIA_ASSET_BYTES
        ):
            raise PublicSiteDeployError("https_readback_request_invalid")
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="readback-", dir=self._state_root
        ) as temporary:
            root = Path(temporary)
            headers_path = root / "headers"
            body_path = root / "body"
            argv = (
                str(CURL_BINARY),
                "--silent",
                "--show-error",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--connect-timeout",
                "5",
                "--max-time",
                "20",
                "--max-filesize",
                str(maximum_bytes),
                "--resolve",
                f"{PUBLIC_HOST}:443:127.0.0.1",
                "--header",
                "Accept-Encoding: identity",
                "--dump-header",
                str(headers_path),
                "--output",
                str(body_path),
                "--write-out",
                "%{http_code}",
                url,
            )
            try:
                result = run_bounded_process(
                    argv,
                    payload=None,
                    cwd=Path("/"),
                    environment={
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    },
                    timeout_seconds=30,
                    max_stdout_bytes=16,
                    max_stderr_bytes=64 * 1024,
                )
                status_text = result.stdout.decode("ascii")
            except Exception:
                raise PublicSiteDeployError("https_readback_failed") from None
            if result.returncode != 0 or re.fullmatch(r"[1-5][0-9]{2}", status_text) is None:
                raise PublicSiteDeployError("https_readback_failed")
            body = _read_site_file(
                body_path,
                maximum_bytes=maximum_bytes,
                code="https_readback_failed",
            )
            header_payload = _read_site_file(
                headers_path,
                maximum_bytes=64 * 1024,
                code="https_readback_failed",
            )
            try:
                blocks = re.split(rb"\r?\n\r?\n", header_payload.strip())
                lines = blocks[-1].decode("iso-8859-1").splitlines()
                parsed_headers: dict[str, list[str]] = {}
                for line in lines[1:]:
                    name, separator, value = line.partition(":")
                    if separator != ":" or not name.strip():
                        raise ValueError
                    parsed_headers.setdefault(name.strip().casefold(), []).append(
                        value.strip()
                    )
            except (UnicodeError, ValueError):
                raise PublicSiteDeployError("https_readback_headers_invalid") from None
            return HttpReadback(
                status=int(status_text),
                headers={name: tuple(values) for name, values in parsed_headers.items()},
                body=body,
            )


def _cache_contains(response: HttpReadback, required: frozenset[str]) -> bool:
    directives: set[str] = set()
    for value in response.values("cache-control"):
        directives.update(part.strip().casefold() for part in value.split(","))
    return required.issubset(directives)


def _public_pointer_matches_authorized_target(
    observed_payload: bytes,
    authorized_payload: bytes,
    trust: PublicPointerTrust,
) -> bool:
    """Allow online freshness renewal, but no signed-target drift.

    The release authorization binds the initially published pointer bytes.
    The Control Plane may subsequently replace only its independently signed
    ``freshness`` member.  A site apply/recovery must therefore accept a
    structurally valid renewed pointer for the same immutable authority while
    still rejecting any release, source, signature, or target substitution.
    """

    values: list[dict[str, Any]] = []
    try:
        for position, payload in enumerate((observed_payload, authorized_payload)):
            value = strict_json_loads(
                payload,
                code="public_pointer_readback_failed",
            )
            if not isinstance(value, dict) or value.get("status") != "published":
                raise ValueError
            validate_public_bootstrap_index(
                value,
                verifier=trust.authority_verifier,
                freshness_verifier=trust.freshness_verifier,
                # The deployment authorization permanently binds the initial
                # pointer bytes.  Its old freshness window may expire after a
                # valid online renewal; only the currently observed document
                # must still be live.
                allow_expired_freshness=position == 1,
            )
            values.append(value)
    except (TypeError, ValueError):
        return False
    observed, authorized = values
    observed_freshness = observed.get("freshness")
    authorized_freshness = authorized.get("freshness")
    if not isinstance(observed_freshness, dict) or not isinstance(
        authorized_freshness, dict
    ):
        return False
    observed_immutable = dict(observed)
    authorized_immutable = dict(authorized)
    observed_immutable.pop("freshness", None)
    authorized_immutable.pop("freshness", None)
    return (
        observed_immutable == authorized_immutable
        and observed_freshness.get("authority_sha256")
        == authorized_freshness.get("authority_sha256")
    )


def _verify_target_readback(
    site: ValidatedSite,
    client: ReadbackClient,
    trust: PublicPointerTrust,
) -> dict[str, Any]:
    index = client.get(PUBLIC_ORIGIN, maximum_bytes=MAX_HTML_BYTES)
    if (
        index.status != 200
        or index.body != site.file("index.html").payload
        or not _cache_contains(index, frozenset({"no-store"}))
    ):
        raise PublicSiteDeployError("public_index_readback_failed")
    pointer = client.get(
        f"{PUBLIC_ORIGIN}public-bootstrap-index.json",
        maximum_bytes=MAX_POINTER_BYTES,
    )
    if (
        pointer.status != 200
        or not _public_pointer_matches_authorized_target(
            pointer.body,
            site.public_index.payload,
            trust,
        )
        or not _cache_contains(pointer, frozenset({"no-store"}))
    ):
        raise PublicSiteDeployError("public_pointer_readback_failed")

    asset_receipts: list[dict[str, Any]] = []
    for value in site.files:
        if value.relative_path in {"index.html", "public-bootstrap-index.json"}:
            continue
        response = client.get(
            f"{PUBLIC_ORIGIN}{value.relative_path}",
            maximum_bytes=max(value.size_bytes, 1),
        )
        if (
            response.status != 200
            or response.body != value.payload
            or not _cache_contains(
                response,
                frozenset({"public", "max-age=31536000", "immutable"}),
            )
        ):
            raise PublicSiteDeployError("public_asset_readback_failed")
        asset_receipts.append(
            {
                "path": value.relative_path,
                "sha256": value.sha256,
                "size_bytes": value.size_bytes,
            }
        )
    admin_identity = site.admin_identity
    if admin_identity is None:
        raise PublicSiteDeployError("admin_deployment_identity_missing")
    validate_admin_deployment_identity(admin_identity, version=site.version)
    admin_index = admin_identity["index"]
    assert isinstance(admin_index, Mapping)
    admin = client.get(
        ADMIN_URL,
        maximum_bytes=int(admin_index["size_bytes"]),
    )
    if (
        admin.status != 200
        or len(admin.body) != admin_index["size_bytes"]
        or _sha256(admin.body) != admin_index["sha256"]
        or not _cache_contains(admin, frozenset({"no-store"}))
        or admin.values("content-security-policy")
        != (ADMIN_CONTENT_SECURITY_POLICY,)
        or admin.values(ADMIN_VERSION_HEADER) != (site.version,)
    ):
        raise PublicSiteDeployError("admin_route_readback_failed")
    admin_asset_receipts: list[dict[str, Any]] = []
    for asset in admin_identity["assets"]:
        assert isinstance(asset, Mapping)
        response = client.get(
            f"https://{PUBLIC_HOST}{asset['path']}",
            maximum_bytes=int(asset["size_bytes"]),
        )
        if (
            response.status != 200
            or len(response.body) != asset["size_bytes"]
            or _sha256(response.body) != asset["sha256"]
            or not _cache_contains(
                response,
                frozenset({"public", "max-age=31536000", "immutable"}),
            )
            or response.values(ADMIN_VERSION_HEADER) != (site.version,)
        ):
            raise PublicSiteDeployError("admin_asset_readback_failed")
        admin_asset_receipts.append(
            {
                "path": asset["path"],
                "sha256": asset["sha256"],
                "size_bytes": asset["size_bytes"],
            }
        )
    health_identity = admin_identity["health"]
    assert isinstance(health_identity, Mapping)
    health = client.get(
        ADMIN_HEALTH_URL,
        maximum_bytes=int(health_identity["body_size_bytes"]),
    )
    if (
        health.status != health_identity["status"]
        or len(health.body) != health_identity["body_size_bytes"]
        or _sha256(health.body) != health_identity["body_sha256"]
        or not _cache_contains(health, frozenset({"no-store"}))
        or health.values(ADMIN_VERSION_HEADER) != (site.version,)
    ):
        raise PublicSiteDeployError("admin_health_readback_failed")
    return {
        "index": {
            "status": index.status,
            "sha256": _sha256(index.body),
            "cache_control": "no-store",
        },
        "pointer": {
            "status": pointer.status,
            "sha256": _sha256(pointer.body),
            "cache_control": "no-store",
        },
        "assets": asset_receipts,
        "admin": {
            "status": admin.status,
            "body_sha256": _sha256(admin.body),
            "version": site.version,
            "cloud_manifest_sha256": admin_identity["cloud_manifest_sha256"],
            "assets": admin_asset_receipts,
            "health": {
                "status": health.status,
                "body_sha256": _sha256(health.body),
            },
        },
    }


def _verify_legacy_readback(expected_index: bytes, client: ReadbackClient) -> None:
    response = client.get(PUBLIC_ORIGIN, maximum_bytes=MAX_HTML_BYTES)
    if response.status != 200 or response.body != expected_index:
        raise PublicSiteDeployError("legacy_site_restore_readback_failed")


def _renameat2(source: Path, target: Path, flag: int, *, code: str) -> None:
    if not sys.platform.startswith("linux"):
        raise PublicSiteDeployError("linux_atomic_rename_unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(target),
            flag,
        )
    except (AttributeError, OSError):
        raise PublicSiteDeployError(code) from None
    if result != 0:
        raise PublicSiteDeployError(code)


def _rename_noreplace(source: Path, target: Path) -> None:
    _renameat2(source, target, 1, code="atomic_noreplace_failed")


def _rename_exchange(source: Path, target: Path) -> None:
    _renameat2(source, target, 2, code="atomic_exchange_failed")


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    payload = _canonical_json(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{hashlib.sha256(payload).hexdigest()[:16]}"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("deployment_state_write_failed") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_canonical_json(path: Path, *, maximum: int, code: str) -> dict[str, Any]:
    payload = _read_site_file(path, maximum_bytes=maximum, code=code)
    try:
        value = strict_json_loads(payload, code=code)
    except ValueError:
        raise PublicSiteDeployError(code) from None
    if not isinstance(value, dict) or payload != _canonical_json(value) + b"\n":
        raise PublicSiteDeployError(code)
    return value


def _journal(paths: DeploymentPaths) -> dict[str, Any] | None:
    if not os.path.lexists(paths.journal_path):
        return None
    value = _read_canonical_json(
        paths.journal_path,
        maximum=128 * 1024,
        code="site_activation_journal_invalid",
    )
    source = value.get("source")
    target = value.get("target")
    try:
        started_at = datetime.fromisoformat(
            str(value.get("started_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        started_at = None
    if (
        set(value)
        != {
            "schema_version",
            "receipt_type",
            "transaction_id",
            "release_id",
            "version",
            "build_digest",
            "site_tree_sha256",
            "direct_receipt_sha256",
            "authorization_sha256",
            "started_at",
            "phase",
            "source",
            "target",
        }
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("receipt_type") != "ecorex-public-site-activation-journal"
        or TRANSACTION_ID.fullmatch(str(value.get("transaction_id") or "")) is None
        or SAFE_RELEASE_ID.fullmatch(str(value.get("release_id") or "")) is None
        or SAFE_VERSION.fullmatch(str(value.get("version") or "")) is None
        or value.get("phase") not in _JOURNAL_PHASES
        or SHA256.fullmatch(str(value.get("build_digest") or "")) is None
        or SHA256.fullmatch(str(value.get("site_tree_sha256") or "")) is None
        or SHA256.fullmatch(str(value.get("direct_receipt_sha256") or "")) is None
        or SHA256.fullmatch(str(value.get("authorization_sha256") or "")) is None
        or started_at is None
        or started_at.tzinfo is None
        or not isinstance(source, dict)
        or set(source)
        != {"kind", "link_target", "index_sha256", "legacy_backup", "exchange_path"}
        or source.get("kind") not in _SOURCE_KINDS
        or not isinstance(target, dict)
        or set(target) != {"slot_path", "current_link_target"}
    ):
        raise PublicSiteDeployError("site_activation_journal_invalid")
    release_id = str(value["release_id"])
    transaction_id = str(value["transaction_id"])
    expected_slot = str(paths.slots_root / release_id)
    expected_exchange = str(
        paths.download_root / f".current.exchange-{transaction_id}"
    )
    expected_backup = str(paths.legacy_root / f"pre-v1-{transaction_id}")
    if (
        target.get("slot_path") != expected_slot
        or target.get("current_link_target") != f"site-slots/{release_id}"
        or source.get("exchange_path") != expected_exchange
        or (
            source.get("kind") == "legacy_directory"
            and source.get("legacy_backup") != expected_backup
        )
        or (
            source.get("kind") != "legacy_directory"
            and source.get("legacy_backup") is not None
        )
        or (
            source.get("kind") == "symlink"
            and not isinstance(source.get("link_target"), str)
        )
        or (
            source.get("kind") != "symlink"
            and source.get("link_target") is not None
        )
        or (
            source.get("kind") == "absent"
            and source.get("index_sha256") is not None
        )
        or (
            source.get("kind") != "absent"
            and SHA256.fullmatch(str(source.get("index_sha256") or "")) is None
        )
    ):
        raise PublicSiteDeployError("site_activation_journal_invalid")
    if source.get("kind") == "symlink":
        try:
            resolved_source = (
                paths.current_path.parent / str(source["link_target"])
            ).resolve(strict=True)
            download = paths.download_root.resolve(strict=True)
        except OSError:
            raise PublicSiteDeployError("site_activation_journal_invalid") from None
        if resolved_source != download and download not in resolved_source.parents:
            raise PublicSiteDeployError("site_activation_journal_invalid")
    return value


def _write_journal(paths: DeploymentPaths, value: Mapping[str, Any]) -> dict[str, Any]:
    _atomic_write_json(paths.journal_path, value, mode=0o600)
    parsed = _journal(paths)
    if parsed is None:
        raise PublicSiteDeployError("site_activation_journal_invalid")
    return parsed


def _advance_journal(
    paths: DeploymentPaths, value: Mapping[str, Any], phase: str
) -> dict[str, Any]:
    if phase not in _JOURNAL_PHASES:
        raise PublicSiteDeployError("site_activation_journal_invalid")
    return _write_journal(paths, {**value, "phase": phase})


def _clear_journal(paths: DeploymentPaths) -> None:
    try:
        paths.journal_path.unlink()
        _fsync_directory(paths.journal_path.parent)
    except FileNotFoundError:
        return
    except OSError:
        raise PublicSiteDeployError("site_activation_journal_clear_failed") from None


def _copy_site_to_slot(
    site: ValidatedSite,
    paths: DeploymentPaths,
    *,
    expected_owner_uid: int | None,
) -> tuple[Path, str]:
    slot = paths.slots_root / site.release_id
    paths.slots_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    _require_plain_directory(
        paths.slots_root,
        code="site_slots_root_invalid",
        expected_owner_uid=expected_owner_uid,
    )
    if os.path.lexists(slot):
        deployed = validate_site_slot(slot, site)
        if deployed.tree_sha256 != site.tree_sha256:
            raise PublicSiteDeployError("site_slot_conflict")
        if expected_owner_uid is not None:
            _validate_slot_layout(slot, paths)
        return slot, "reused"

    temporary = paths.slots_root / (
        f".{site.release_id}.tmp-{os.getpid()}-{site.tree_sha256[:16]}"
    )
    try:
        temporary.mkdir(mode=0o755)
        (temporary / "assets").mkdir(mode=0o755)
        if expected_owner_uid is not None:
            os.chown(temporary, 0, STATIC_READ_GID, follow_symlinks=False)
            os.chown(
                temporary / "assets",
                0,
                STATIC_READ_GID,
                follow_symlinks=False,
            )
        for value in site.files:
            target = temporary / PurePosixPath(value.relative_path)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value.payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(target, 0o444)
            if expected_owner_uid is not None:
                os.chown(target, 0, STATIC_READ_GID, follow_symlinks=False)
        _fsync_directory(temporary / "assets")
        _fsync_directory(temporary)
        copied = validate_site_slot(temporary, site)
        if copied.tree_sha256 != site.tree_sha256:
            raise PublicSiteDeployError("site_slot_copy_mismatch")
        _rename_noreplace(temporary, slot)
        _fsync_directory(paths.slots_root)
        if expected_owner_uid is not None:
            _validate_slot_layout(slot, paths)
        return slot, "created"
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("site_slot_copy_failed") from None
    finally:
        if temporary.exists() and temporary.is_dir():
            # A temporary is never public and contains only files created by
            # this transaction.  Do not follow links during cleanup.
            for child in sorted(temporary.rglob("*"), reverse=True):
                try:
                    if child.is_dir() and not child.is_symlink():
                        child.rmdir()
                    else:
                        child.unlink()
                except FileNotFoundError:
                    pass
            try:
                temporary.rmdir()
            except FileNotFoundError:
                pass


def validate_site_slot(slot: Path, expected: ValidatedSite) -> ValidatedSite:
    """Validate one immutable slot against the already validated source bytes."""

    try:
        metadata = slot.lstat()
    except OSError:
        raise PublicSiteDeployError("site_slot_invalid") from None
    if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicSiteDeployError("site_slot_invalid")
    observed: list[SiteFile] = []
    expected_paths = {value.relative_path for value in expected.files}
    observed_directories: set[str] = set()
    try:
        entries = []
        for root, directories, files in os.walk(slot, followlinks=False):
            root_path = Path(root)
            for directory in directories:
                entry = root_path / directory
                entry_metadata = entry.lstat()
                if _linked(entry_metadata) or not stat.S_ISDIR(entry_metadata.st_mode):
                    raise PublicSiteDeployError("site_slot_link_forbidden")
                observed_directories.add(entry.relative_to(slot).as_posix())
            for name in files:
                entries.append(root_path / name)
    except OSError:
        raise PublicSiteDeployError("site_slot_invalid") from None
    relative_paths = {
        path.relative_to(slot).as_posix()
        for path in entries
    }
    if relative_paths != expected_paths or observed_directories != {"assets"}:
        raise PublicSiteDeployError("site_slot_file_set_mismatch")
    for expected_file in expected.files:
        payload = _read_site_file(
            slot / PurePosixPath(expected_file.relative_path),
            maximum_bytes=max(expected_file.size_bytes, 1),
            code="site_slot_file_invalid",
        )
        if payload != expected_file.payload:
            raise PublicSiteDeployError("site_slot_file_mismatch")
        observed.append(
            SiteFile(expected_file.relative_path, payload, _sha256(payload))
        )
    return dataclasses.replace(expected, root=slot, files=tuple(observed))


def _validate_slot_layout(slot: Path, paths: DeploymentPaths) -> None:
    try:
        device = paths.download_root.lstat().st_dev
        directories = (slot, slot / "assets")
        for directory in directories:
            metadata = directory.lstat()
            if (
                _linked(metadata)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != STATIC_READ_GID
                or stat.S_IMODE(metadata.st_mode) != 0o755
                or metadata.st_dev != device
            ):
                raise OSError
        for root, directory_names, file_names in os.walk(slot, followlinks=False):
            if Path(root) == slot and set(directory_names) != {"assets"}:
                raise OSError
            if Path(root) == slot / "assets" and directory_names:
                raise OSError
            for name in file_names:
                metadata = (Path(root) / name).lstat()
                if (
                    _linked(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != 0
                    or metadata.st_gid != STATIC_READ_GID
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                    or metadata.st_dev != device
                    or getattr(metadata, "st_nlink", 1) != 1
                ):
                    raise OSError
    except OSError:
        raise PublicSiteDeployError("site_slot_layout_invalid") from None


def _source_state(paths: DeploymentPaths, transaction_id: str) -> dict[str, Any]:
    current = paths.current_path
    exchange = paths.download_root / f".current.exchange-{transaction_id}"
    backup = paths.legacy_root / f"pre-v1-{transaction_id}"
    if not os.path.lexists(current):
        return {
            "kind": "absent",
            "link_target": None,
            "index_sha256": None,
            "legacy_backup": None,
            "exchange_path": str(exchange),
        }
    metadata = current.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        link_target = os.readlink(current)
        resolved = (current.parent / link_target).resolve(strict=True)
        download = paths.download_root.resolve(strict=True)
        if resolved != download and download not in resolved.parents:
            raise PublicSiteDeployError("current_symlink_escapes_product_root")
        index = _read_site_file(
            resolved / "index.html",
            maximum_bytes=MAX_HTML_BYTES,
            code="current_site_invalid",
        )
        return {
            "kind": "symlink",
            "link_target": link_target,
            "index_sha256": _sha256(index),
            "legacy_backup": None,
            "exchange_path": str(exchange),
        }
    if stat.S_ISDIR(metadata.st_mode) and not _linked(metadata):
        index = _read_site_file(
            current / "index.html",
            maximum_bytes=MAX_HTML_BYTES,
            code="legacy_current_site_invalid",
        )
        return {
            "kind": "legacy_directory",
            "link_target": None,
            "index_sha256": _sha256(index),
            "legacy_backup": str(backup),
            "exchange_path": str(exchange),
        }
    raise PublicSiteDeployError("current_site_type_invalid")


def _new_journal(
    site: ValidatedSite,
    paths: DeploymentPaths,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    # The exchange path suffix includes the complete site-<hex> transaction.
    transaction_id = Path(str(source["exchange_path"])).name.removeprefix(
        ".current.exchange-"
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "ecorex-public-site-activation-journal",
        "transaction_id": transaction_id,
        "release_id": site.release_id,
        "version": site.version,
        "build_digest": site.build_digest,
        "site_tree_sha256": site.tree_sha256,
        "direct_receipt_sha256": site.direct_receipt_sha256,
        "authorization_sha256": site.authorization_sha256,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": "prepared",
        "source": dict(source),
        "target": {
            "slot_path": str(paths.slots_root / site.release_id),
            "current_link_target": f"site-slots/{site.release_id}",
        },
    }
    return _write_journal(paths, value)


def _make_next_link(paths: DeploymentPaths, journal: Mapping[str, Any]) -> Path:
    exchange = Path(str(journal["source"]["exchange_path"]))
    if os.path.lexists(exchange):
        raise PublicSiteDeployError("site_exchange_path_conflict")
    try:
        os.symlink(str(journal["target"]["current_link_target"]), exchange)
        if sys.platform.startswith("linux"):
            os.lchown(exchange, 0, STATIC_READ_GID)
        _fsync_directory(paths.download_root)
    except OSError:
        raise PublicSiteDeployError("site_current_link_create_failed") from None
    return exchange


def _switch_current(paths: DeploymentPaths, journal: Mapping[str, Any]) -> None:
    exchange = _make_next_link(paths, journal)
    source = journal["source"]
    try:
        if source["kind"] == "legacy_directory":
            paths.legacy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = Path(str(source["legacy_backup"]))
            if os.path.lexists(backup):
                raise PublicSiteDeployError("legacy_backup_conflict")
            _rename_exchange(paths.current_path, exchange)
            _fsync_directory(paths.download_root)
            # current is now the candidate symlink; exchange is the old
            # directory.  Moving it to the hidden legacy root is no-clobber.
            _rename_noreplace(exchange, backup)
            _fsync_directory(paths.download_root)
            _fsync_directory(paths.legacy_root)
        else:
            os.replace(exchange, paths.current_path)
        _fsync_directory(paths.download_root)
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("site_current_switch_failed") from None
    finally:
        if os.path.islink(exchange):
            exchange.unlink()


def _current_points_to_target(paths: DeploymentPaths, journal: Mapping[str, Any]) -> bool:
    try:
        return (
            paths.current_path.is_symlink()
            and os.readlink(paths.current_path)
            == journal["target"]["current_link_target"]
            and paths.current_path.resolve(strict=True)
            == Path(str(journal["target"]["slot_path"])).resolve(strict=True)
        )
    except OSError:
        return False


def _current_is_source(paths: DeploymentPaths, journal: Mapping[str, Any]) -> bool:
    source = journal["source"]
    if source["kind"] == "absent":
        return not os.path.lexists(paths.current_path)
    if source["kind"] == "symlink":
        try:
            return paths.current_path.is_symlink() and os.readlink(
                paths.current_path
            ) == source["link_target"]
        except OSError:
            return False
    if source["kind"] == "legacy_directory":
        try:
            metadata = paths.current_path.lstat()
            if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
                return False
            index = _read_site_file(
                paths.current_path / "index.html",
                maximum_bytes=MAX_HTML_BYTES,
                code="legacy_current_site_invalid",
            )
            return _sha256(index) == source["index_sha256"]
        except (OSError, PublicSiteDeployError):
            return False
    return False


def _restore_source(paths: DeploymentPaths, journal: Mapping[str, Any]) -> None:
    source = journal["source"]
    transaction_id = str(journal["transaction_id"])
    restore_link = paths.download_root / f".current.restore-{transaction_id}"
    try:
        if source["kind"] == "absent":
            if os.path.lexists(paths.current_path):
                if not paths.current_path.is_symlink():
                    raise PublicSiteDeployError("site_restore_conflict")
                paths.current_path.unlink()
        elif source["kind"] == "symlink":
            if os.path.lexists(restore_link):
                raise PublicSiteDeployError("site_restore_conflict")
            os.symlink(str(source["link_target"]), restore_link)
            if sys.platform.startswith("linux"):
                os.lchown(restore_link, 0, STATIC_READ_GID)
            os.replace(restore_link, paths.current_path)
        else:
            backup = Path(str(source["legacy_backup"]))
            exchange = Path(str(source["exchange_path"]))
            legacy = backup if backup.is_dir() and not backup.is_symlink() else exchange
            if not legacy.is_dir() or legacy.is_symlink():
                if _current_is_source(paths, journal):
                    return
                raise PublicSiteDeployError("legacy_backup_missing")
            if os.path.lexists(paths.current_path):
                if not paths.current_path.is_symlink():
                    raise PublicSiteDeployError("site_restore_conflict")
                _rename_exchange(paths.current_path, legacy)
                _fsync_directory(paths.download_root)
                _fsync_directory(legacy.parent)
                # The candidate symlink is now at the former legacy path.
                legacy.unlink()
                _fsync_directory(legacy.parent)
            else:
                _rename_noreplace(legacy, paths.current_path)
                _fsync_directory(legacy.parent)
        _fsync_directory(paths.download_root)
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("site_restore_failed") from None
    finally:
        if os.path.islink(restore_link):
            restore_link.unlink()


def _settle_or_clean_exchange(
    paths: DeploymentPaths,
    journal: Mapping[str, Any],
    *,
    target_is_active: bool,
) -> None:
    """Resolve the only hidden path which can survive a process crash."""

    source = journal["source"]
    exchange = Path(str(source["exchange_path"]))
    if not os.path.lexists(exchange):
        return
    if exchange.is_symlink():
        try:
            if os.readlink(exchange) != journal["target"]["current_link_target"]:
                raise PublicSiteDeployError("site_exchange_path_conflict")
            exchange.unlink()
            _fsync_directory(paths.download_root)
            return
        except OSError:
            raise PublicSiteDeployError("site_exchange_path_conflict") from None
    if source["kind"] == "legacy_directory" and exchange.is_dir():
        if not target_is_active:
            # Source restoration consumes this directory via atomic exchange.
            return
        backup = Path(str(source["legacy_backup"]))
        if os.path.lexists(backup):
            raise PublicSiteDeployError("legacy_backup_conflict")
        _rename_noreplace(exchange, backup)
        _fsync_directory(paths.download_root)
        _fsync_directory(paths.legacy_root)
        return
    raise PublicSiteDeployError("site_exchange_path_conflict")


def _verify_source_after_restore(
    paths: DeploymentPaths,
    journal: Mapping[str, Any],
    client: ReadbackClient,
) -> None:
    source = journal["source"]
    if not _current_is_source(paths, journal):
        raise PublicSiteDeployError("site_restore_verification_failed")
    if source["kind"] == "absent":
        response = client.get(PUBLIC_ORIGIN, maximum_bytes=MAX_HTML_BYTES)
        if response.status not in {403, 404}:
            raise PublicSiteDeployError("site_restore_readback_failed")
        return
    resolved = paths.current_path.resolve(strict=True)
    expected = _read_site_file(
        resolved / "index.html",
        maximum_bytes=MAX_HTML_BYTES,
        code="site_restore_verification_failed",
    )
    if _sha256(expected) != source["index_sha256"]:
        raise PublicSiteDeployError("site_restore_verification_failed")
    _verify_legacy_readback(expected, client)


def _receipt_value(
    site: ValidatedSite,
    journal: Mapping[str, Any],
    slot_action: str,
    readback: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "ecorex-public-site-deployment",
        "status": "passed",
        "release_id": site.release_id,
        "version": site.version,
        "build_digest": site.build_digest,
        "site_tree_sha256": site.tree_sha256,
        "public_index_sha256": site.public_index_sha256,
        "direct_deployable_receipt_sha256": site.direct_receipt_sha256,
        "deployment_authorization_sha256": site.authorization_sha256,
        "publication_receipt_sha256": site.publication_receipt_sha256,
        "target": PUBLIC_ORIGIN,
        "slot": str(journal["target"]["slot_path"]),
        "slot_action": slot_action,
        "previous_target_type": journal["source"]["kind"],
        "transaction_id": journal["transaction_id"],
        "activated_at": journal["started_at"],
        "nginx": {"config_test": "passed", "reload": "passed"},
        "readback": dict(readback),
    }


def _write_receipt(
    paths: DeploymentPaths,
    site: ValidatedSite,
    journal: Mapping[str, Any],
    slot_action: str,
    readback: Mapping[str, Any],
) -> tuple[Path, str]:
    receipt = _receipt_value(site, journal, slot_action, readback)
    payload = _canonical_json(receipt) + b"\n"
    path = paths.receipt_root / f"{site.release_id}.json"
    paths.receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.lexists(path):
        existing = _read_site_file(
            path,
            maximum_bytes=256 * 1024,
            code="site_deployment_receipt_conflict",
        )
        try:
            value = strict_json_loads(existing, code="site_deployment_receipt_conflict")
        except ValueError:
            raise PublicSiteDeployError("site_deployment_receipt_conflict") from None
        if (
            not isinstance(value, dict)
            or value.get("release_id") != site.release_id
            or value.get("site_tree_sha256") != site.tree_sha256
            or value.get("transaction_id") != journal["transaction_id"]
            or value.get("status") != "passed"
        ):
            raise PublicSiteDeployError("site_deployment_receipt_conflict")
        return path, _sha256(existing)
    temporary = paths.receipt_root / (
        f".{site.release_id}.tmp-{journal['transaction_id']}"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_noreplace(temporary, path)
        _fsync_directory(paths.receipt_root)
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("site_deployment_receipt_write_failed") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path, _sha256(payload)


def _acquire_lock(path: Path) -> int:
    if fcntl is None or not sys.platform.startswith("linux"):
        raise PublicSiteDeployError("linux_flock_unavailable")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        raise PublicSiteDeployError("product_deploy_lock_unavailable") from None


def _release_lock(descriptor: int) -> None:
    assert fcntl is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _normalize_directory(
    path: Path,
    *,
    mode: int,
    gid: int,
    device: int | None,
    create: bool,
) -> os.stat_result:
    try:
        if not os.path.lexists(path):
            if not create:
                raise OSError
            path.mkdir(mode=mode)
        before = path.lstat()
        if _linked(before) or not stat.S_ISDIR(before.st_mode):
            raise OSError
        os.chown(path, 0, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        after = path.lstat()
        if (
            _linked(after)
            or not stat.S_ISDIR(after.st_mode)
            or after.st_uid != 0
            or after.st_gid != gid
            or stat.S_IMODE(after.st_mode) != mode
            or (device is not None and after.st_dev != device)
            or path.resolve(strict=True) != path.absolute()
        ):
            raise OSError
        _fsync_directory(path.parent)
        return after
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("production_site_layout_invalid") from None


def _normalize_staging_tree(
    paths: DeploymentPaths,
    release_id: str,
    *,
    device: int,
) -> None:
    release = paths.staging_root / release_id
    site = release / "site"
    assets = site / "assets"
    for directory in (release, site, assets):
        _normalize_directory(
            directory,
            mode=0o700,
            gid=0,
            device=device,
            create=False,
        )
    try:
        if {entry.name for entry in release.iterdir()} != {
            "site",
            "direct-deployable.json",
            "deployment-authorization.json",
        }:
            raise OSError
        root_names = {entry.name for entry in site.iterdir()}
        if "assets" not in root_names:
            raise OSError
        file_paths = [
            entry for entry in site.iterdir() if entry.name != "assets"
        ] + list(assets.iterdir()) + [
            release / "direct-deployable.json",
            release / "deployment-authorization.json",
        ]
        for path in file_paths:
            metadata = path.lstat()
            if (
                _linked(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or getattr(metadata, "st_nlink", 1) != 1
                or metadata.st_dev != device
            ):
                raise OSError
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, 0o600, follow_symlinks=False)
            current = path.lstat()
            if (
                current.st_uid != 0
                or current.st_gid != 0
                or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_dev != device
            ):
                raise OSError
        _fsync_directory(assets)
        _fsync_directory(site)
        _fsync_directory(release)
    except PublicSiteDeployError:
        raise
    except OSError:
        raise PublicSiteDeployError("production_staging_layout_invalid") from None


def _validate_production_public_pointer_layout(
    paths: DeploymentPaths,
    *,
    device: int,
) -> None:
    """Validate the Control-Plane-owned pointer without taking ownership."""

    try:
        import grp
        import pwd

        owner_uid = pwd.getpwnam("ecorex-cloud").pw_uid
        storage_gid = grp.getgrnam("ecorex-storage").gr_gid
        root = paths.public_pointer_path.parent
        root_metadata = root.lstat()
        pointer_metadata = paths.public_pointer_path.lstat()
        if (
            _linked(root_metadata)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root.resolve(strict=True) != root.absolute()
            or root_metadata.st_uid != owner_uid
            or root_metadata.st_gid != storage_gid
            or stat.S_IMODE(root_metadata.st_mode) != 0o755
            or root_metadata.st_dev != device
            or _linked(pointer_metadata)
            or not stat.S_ISREG(pointer_metadata.st_mode)
            or getattr(pointer_metadata, "st_nlink", 1) != 1
            or pointer_metadata.st_uid != owner_uid
            or stat.S_IMODE(pointer_metadata.st_mode) != 0o644
            or pointer_metadata.st_dev != device
        ):
            raise OSError
    except (ImportError, KeyError, OSError):
        raise PublicSiteDeployError(
            "production_public_pointer_layout_invalid"
        ) from None


def _verify_local_public_pointer(
    paths: DeploymentPaths,
    site: ValidatedSite,
    trust: PublicPointerTrust,
) -> None:
    payload = _read_site_file(
        paths.public_pointer_path,
        maximum_bytes=MAX_POINTER_BYTES,
        code="production_public_pointer_identity_invalid",
    )
    if not _public_pointer_matches_authorized_target(
        payload,
        site.public_index.payload,
        trust,
    ):
        raise PublicSiteDeployError("production_public_pointer_identity_invalid")


def _prepare_production_layout(
    paths: DeploymentPaths,
    release_id: str,
) -> None:
    """Take ownership once, then validate every mutable public-site boundary."""

    try:
        root_before = paths.download_root.lstat()
        if _linked(root_before) or not stat.S_ISDIR(root_before.st_mode):
            raise OSError
        os.chown(paths.download_root, 0, STATIC_READ_GID, follow_symlinks=False)
        os.chmod(paths.download_root, 0o755, follow_symlinks=False)
        root = paths.download_root.lstat()
        if (
            root.st_uid != 0
            or root.st_gid != STATIC_READ_GID
            or stat.S_IMODE(root.st_mode) != 0o755
            or paths.download_root.resolve(strict=True)
            != paths.download_root.absolute()
        ):
            raise OSError
    except OSError:
        raise PublicSiteDeployError("product_download_root_takeover_failed") from None
    device = root.st_dev
    _normalize_directory(
        paths.slots_root,
        mode=0o755,
        gid=STATIC_READ_GID,
        device=device,
        create=True,
    )
    _normalize_directory(
        paths.staging_root,
        mode=0o700,
        gid=0,
        device=device,
        create=False,
    )
    _normalize_directory(
        paths.legacy_root,
        mode=0o700,
        gid=0,
        device=device,
        create=True,
    )
    _validate_production_public_pointer_layout(paths, device=device)
    _normalize_staging_tree(paths, release_id, device=device)
    if os.path.lexists(paths.current_path):
        try:
            current = paths.current_path.lstat()
            if stat.S_ISLNK(current.st_mode):
                os.lchown(paths.current_path, 0, STATIC_READ_GID)
                current = paths.current_path.lstat()
                resolved = paths.current_path.resolve(strict=True)
                resolved_metadata = resolved.lstat()
                if (
                    current.st_uid != 0
                    or current.st_gid != STATIC_READ_GID
                    or current.st_dev != device
                    or _linked(resolved_metadata)
                    or not stat.S_ISDIR(resolved_metadata.st_mode)
                    or resolved_metadata.st_dev != device
                    or (
                        resolved != paths.download_root
                        and paths.download_root.resolve(strict=True)
                        not in resolved.parents
                    )
                ):
                    raise OSError
            elif stat.S_ISDIR(current.st_mode) and not _linked(current):
                # One legacy entity-directory is accepted only so the first v1
                # activation can exchange it atomically for ``current``.
                os.chown(paths.current_path, 0, STATIC_READ_GID, follow_symlinks=False)
                os.chmod(paths.current_path, 0o755, follow_symlinks=False)
                normalized = paths.current_path.lstat()
                if (
                    normalized.st_uid != 0
                    or normalized.st_gid != STATIC_READ_GID
                    or stat.S_IMODE(normalized.st_mode) != 0o755
                    or normalized.st_dev != device
                ):
                    raise OSError
            else:
                raise OSError
        except OSError:
            raise PublicSiteDeployError("production_current_layout_invalid") from None
    _normalize_directory(
        paths.state_root,
        mode=0o700,
        gid=0,
        device=None,
        create=True,
    )
    _fsync_directory(paths.download_root)


def _recover(
    site: ValidatedSite,
    paths: DeploymentPaths,
    controller: ServerController,
    client: ReadbackClient,
    trust: PublicPointerTrust,
) -> dict[str, Any] | None:
    journal = _journal(paths)
    if journal is None:
        return None
    if (
        journal["release_id"] != site.release_id
        or journal["site_tree_sha256"] != site.tree_sha256
        or journal["direct_receipt_sha256"] != site.direct_receipt_sha256
        or journal["authorization_sha256"] != site.authorization_sha256
    ):
        raise PublicSiteDeployError("pending_site_activation_differs")
    slot = Path(str(journal["target"]["slot_path"]))
    validate_site_slot(slot, site)
    if _current_points_to_target(paths, journal):
        try:
            _settle_or_clean_exchange(
                paths, journal, target_is_active=True
            )
            controller.reload()
            readback = _verify_target_readback(site, client, trust)
            receipt, receipt_sha = _write_receipt(
                paths, site, journal, "recovered", readback
            )
            _clear_journal(paths)
            return {
                "resolution": "target",
                "receipt": str(receipt),
                "receipt_sha256": receipt_sha,
            }
        except PublicSiteDeployError:
            _restore_source(paths, journal)
            controller.reload()
            _verify_source_after_restore(paths, journal, client)
            _clear_journal(paths)
            return {"resolution": "source"}
    if not _current_is_source(paths, journal):
        _restore_source(paths, journal)
    _settle_or_clean_exchange(paths, journal, target_is_active=False)
    controller.reload()
    _verify_source_after_restore(paths, journal, client)
    _clear_journal(paths)
    return {"resolution": "source"}


def _existing_receipt(
    paths: DeploymentPaths,
    site: ValidatedSite,
) -> tuple[Path, str] | None:
    path = paths.receipt_root / f"{site.release_id}.json"
    if not os.path.lexists(path):
        return None
    payload = _read_site_file(
        path,
        maximum_bytes=256 * 1024,
        code="site_deployment_receipt_conflict",
    )
    try:
        value = strict_json_loads(payload, code="site_deployment_receipt_conflict")
    except ValueError:
        raise PublicSiteDeployError("site_deployment_receipt_conflict") from None
    expected_keys = {
        "schema_version",
        "receipt_type",
        "status",
        "release_id",
        "version",
        "build_digest",
        "site_tree_sha256",
        "public_index_sha256",
        "direct_deployable_receipt_sha256",
        "deployment_authorization_sha256",
        "publication_receipt_sha256",
        "target",
        "slot",
        "slot_action",
        "previous_target_type",
        "transaction_id",
        "activated_at",
        "nginx",
        "readback",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("receipt_type") != "ecorex-public-site-deployment"
        or value.get("status") != "passed"
        or value.get("release_id") != site.release_id
        or value.get("version") != site.version
        or value.get("build_digest") != site.build_digest
        or value.get("site_tree_sha256") != site.tree_sha256
        or value.get("public_index_sha256") != site.public_index_sha256
        or value.get("direct_deployable_receipt_sha256")
        != site.direct_receipt_sha256
        or value.get("deployment_authorization_sha256")
        != site.authorization_sha256
        or value.get("target") != PUBLIC_ORIGIN
    ):
        raise PublicSiteDeployError("site_deployment_receipt_conflict")
    return path, _sha256(payload)


def _current_points_to_site(paths: DeploymentPaths, site: ValidatedSite) -> bool:
    try:
        return (
            paths.current_path.is_symlink()
            and os.readlink(paths.current_path) == f"site-slots/{site.release_id}"
            and paths.current_path.resolve(strict=True)
            == (paths.slots_root / site.release_id).resolve(strict=True)
        )
    except OSError:
        return False


def plan(
    release_id: str,
    *,
    paths: DeploymentPaths = DeploymentPaths(),
    expected_owner_uid: int | None = 0,
) -> dict[str, Any]:
    site = validate_staged_site(
        release_id,
        paths=paths,
        expected_owner_uid=expected_owner_uid,
    )
    slot = paths.slots_root / release_id
    slot_action = "create"
    if os.path.lexists(slot):
        validate_site_slot(slot, site)
        if expected_owner_uid is not None:
            _validate_slot_layout(slot, paths)
        slot_action = "reuse"
    pending = _journal(paths)
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "ecorex-public-site-deployment-plan",
        "status": "planned",
        "mutation_performed": False,
        "target": PUBLIC_ORIGIN,
        "release_id": site.release_id,
        "version": site.version,
        "build_digest": site.build_digest,
        "site_tree_sha256": site.tree_sha256,
        "public_index_sha256": site.public_index_sha256,
        "direct_deployable_receipt_sha256": site.direct_receipt_sha256,
        "deployment_authorization_sha256": site.authorization_sha256,
        "slot": str(slot),
        "slot_action": slot_action,
        "pending_recovery": pending is not None,
        "fixed_binaries": [
            str(NGINX_BINARY),
            str(SYSTEMCTL_BINARY),
            str(CURL_BINARY),
        ],
    }


def apply(
    release_id: str,
    *,
    confirm_target: str,
    paths: DeploymentPaths = DeploymentPaths(),
    controller: ServerController | None = None,
    client: ReadbackClient | None = None,
    expected_owner_uid: int | None = 0,
    enforce_server_fence: bool = True,
) -> dict[str, Any]:
    """Atomically deploy one validated site and return its typed receipt."""

    if confirm_target != PUBLIC_ORIGIN:
        raise PublicSiteDeployError("deployment_target_confirmation_mismatch")
    if enforce_server_fence and (
        not sys.platform.startswith("linux")
        or not hasattr(os, "geteuid")
        or os.geteuid() != 0
        or paths != DeploymentPaths()
    ):
        raise PublicSiteDeployError("production_server_fence_failed")
    descriptor = _acquire_lock(paths.lock_path)
    try:
        if enforce_server_fence:
            _prepare_production_layout(paths, release_id)
        site = validate_staged_site(
            release_id,
            paths=paths,
            expected_owner_uid=expected_owner_uid,
        )
        trust = _read_public_pointer_trust(
            paths,
            expected_owner_uid=expected_owner_uid,
        )
        if enforce_server_fence:
            _verify_local_public_pointer(paths, site, trust)
        controller = controller or FixedNginxController()
        client = client or FixedCurlReadback(paths.state_root)
        recovered = _recover(site, paths, controller, client, trust)
        if recovered is not None and recovered["resolution"] == "target":
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "passed",
                "release_id": site.release_id,
                "recovered": True,
                **recovered,
            }
        controller.validate()
        _slot, slot_action = _copy_site_to_slot(
            site,
            paths,
            expected_owner_uid=expected_owner_uid,
        )
        existing_receipt = _existing_receipt(paths, site)
        current_is_site = _current_points_to_site(paths, site)
        if existing_receipt is not None and not current_is_site:
            raise PublicSiteDeployError("site_deployment_receipt_state_mismatch")
        if existing_receipt is not None and current_is_site:
            controller.reload()
            _verify_target_readback(site, client, trust)
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "passed",
                "release_id": site.release_id,
                "version": site.version,
                "site_tree_sha256": site.tree_sha256,
                "target": PUBLIC_ORIGIN,
                "slot": str(paths.slots_root / site.release_id),
                "receipt": str(existing_receipt[0]),
                "receipt_sha256": existing_receipt[1],
                "recovered": False,
                "idempotent": True,
            }
        transaction_id = f"site-{os.urandom(16).hex()}"
        source = _source_state(paths, transaction_id)
        journal = _new_journal(site, paths, source)
        try:
            _switch_current(paths, journal)
            journal = _advance_journal(paths, journal, "current_switched")
            controller.reload()
            readback = _verify_target_readback(site, client, trust)
            journal = _advance_journal(paths, journal, "verified")
            receipt, receipt_sha = _write_receipt(
                paths, site, journal, slot_action, readback
            )
            _clear_journal(paths)
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "passed",
                "release_id": site.release_id,
                "version": site.version,
                "site_tree_sha256": site.tree_sha256,
                "target": PUBLIC_ORIGIN,
                "slot": str(paths.slots_root / site.release_id),
                "receipt": str(receipt),
                "receipt_sha256": receipt_sha,
                "recovered": False,
            }
        except PublicSiteDeployError as error:
            if error.code == "site_activation_journal_clear_failed":
                # The target is fully verified and its receipt is durable.  A
                # failed commit-marker removal is recovered as target on the
                # next invocation; rolling back here would make that receipt a
                # false statement.
                raise PublicSiteDeployError(
                    "site_activation_recovery_required"
                ) from None
            try:
                _restore_source(paths, journal)
                controller.reload()
                _verify_source_after_restore(paths, journal, client)
                _clear_journal(paths)
            except PublicSiteDeployError:
                # Durable journal intentionally remains for the next invocation.
                raise PublicSiteDeployError("site_activation_recovery_required") from None
            raise
    finally:
        _release_lock(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy-v1-public-site",
        description="plan or atomically deploy the fixed EcoreX public Web site",
    )
    parser.add_argument("--release-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-target")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.dry_run:
            if args.confirm_target is not None:
                raise PublicSiteDeployError("dry_run_target_confirmation_forbidden")
            result = plan(args.release_id)
        else:
            if args.confirm_target is None:
                raise PublicSiteDeployError("deployment_target_confirmation_required")
            result = apply(args.release_id, confirm_target=args.confirm_target)
    except PublicSiteDeployError as error:
        print(
            json.dumps(
                {"ok": False, "code": error.code},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"ok": True, **result},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


__all__ = [
    "ADMIN_URL",
    "ADMIN_HEALTH_URL",
    "ADMIN_CONTENT_SECURITY_POLICY",
    "ADMIN_VERSION_HEADER",
    "DeploymentPaths",
    "FixedCurlReadback",
    "FixedNginxController",
    "HttpReadback",
    "PUBLIC_ORIGIN",
    "PUBLIC_SITE_AUTHORIZATION_DOMAIN",
    "PublicSiteDeployError",
    "ValidatedSite",
    "apply",
    "build_admin_deployment_identity",
    "main",
    "plan",
    "public_site_authorization_payload",
    "public_site_authorization_signing_bytes",
    "sign_public_site_authorization",
    "validate_site_slot",
    "validate_staged_site",
    "validate_admin_deployment_identity",
    "verify_public_site_authorization",
]
