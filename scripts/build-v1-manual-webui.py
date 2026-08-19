#!/usr/bin/env python3
"""Build the signed e-Mate 2.0 Runtime seed for unsigned desktop packages.

This is the narrow successor to the v0.3.2 manual WebUI release.  It reuses
the already published native/Python dependency closure, replaces product code
and Web assets from one exact source commit, rebuilds Bootstrap, and signs the
inner release with an ephemeral in-memory Ed25519 key.  It never creates an
app/DMG/PKG, invokes a developer identity, notarizes, uploads, or publishes.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex import __version__  # noqa: E402
from ecorex.integration.pack_python import (  # noqa: E402
    build_pack_python_manifest,
    resolve_pack_python,
)
from ecorex.product_version import (  # noqa: E402
    is_stable_release_version,
    stable_release_sequence,
)
from ecorex.pack_catalog import (  # noqa: E402
    COW_RUNTIME_SOURCE_ROOTS,
    required_capability_pack_projection,
)
from ecorex.release import (  # noqa: E402
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    MAX_CORE_ARCHIVE_BYTES,
    MAX_CORE_EXPANDED_BYTES,
    MAX_RELEASE_METADATA_BYTES,
    MAX_RELEASE_SBOM_BYTES,
    ReleaseBuildSpec,
    ReleaseBuilder,
    WebBundleBuildInput,
)
from ecorex.release.build_dependency_lock import active_lock_versions  # noqa: E402
from ecorex.release.dependency_lock import DependencyLockError  # noqa: E402
from ecorex.release.candidate import PACK_SERVICES, PACK_TOOLS  # noqa: E402
from ecorex.release.dependency_lock import load_dependency_lock_manifest  # noqa: E402
from ecorex.release.windows_webui import _verify_webui_contract  # noqa: E402
from ecorex.update import (  # noqa: E402
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
)


BASE_VERSION = "0.3.2"
BASE_RELEASE_ID = "release-stable-76e2ba3641d80b7510d1c5e0"
BASE_PACKAGES = {
    "windows": (
        "EcoreX_0.3.2-webui-windows-x64.zip",
        "29dbececc3f3d9fb59ee9f01880735abef80e9acd081fca23810f2ba428f3ffa",
    ),
    "macos": (
        "EcoreX_0.3.2-webui-macos-universal.zip",
        "a495ad619198e623298bf79e88618f9b397e61993772059eb1d79183037e5754",
    ),
}
TARGETS = (("windows", "x64"), ("macos", "arm64"), ("macos", "x64"))
COMMIT = re.compile(r"^[0-9a-f]{40}$")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
MAX_MEMBERS = 50_000
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
WINDOWS_PACKAGE = f"e-Mate_{__version__}-runtime-windows-x64.zip"
MACOS_PACKAGE = f"e-Mate_{__version__}-runtime-macos-universal.zip"
RECEIPT_SCHEMA = "emate.desktop-runtime-build-receipt.v2"
PREDECESSOR_TRUST_SCHEMA = "emate.desktop-predecessor-trust.v1"
PURE_RUNTIME_OVERLAYS = {
    "charset-normalizer": ("charset_normalizer", "3.4.9"),
    "lark-channel-sdk": ("lark_channel", "1.2.0"),
    "qrcode": ("qrcode", "8.2"),
    "requests": ("requests", "2.32.5"),
    "requests-toolbelt": ("requests_toolbelt", "1.0.0"),
    "urllib3": ("urllib3", "2.7.0"),
}
class ManualWebUIBuildError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise ManualWebUIBuildError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_release_evidence_bounds(metadata_path: Path, sbom_path: Path) -> None:
    if (
        not 1 <= metadata_path.stat().st_size <= MAX_RELEASE_METADATA_BYTES
        or not 1 <= sbom_path.stat().st_size <= MAX_RELEASE_SBOM_BYTES
    ):
        _fail("manual_webui_release_evidence_invalid")


def _verify_release_core_bounds(built: Any) -> None:
    for artifact in built.manifest.artifacts:
        if not artifact.artifact_id.startswith("core-"):
            continue
        path = built.artifact_paths[artifact.artifact_id]
        try:
            archive_size = path.stat().st_size
            with zipfile.ZipFile(path) as archive:
                expanded_size = sum(member.file_size for member in archive.infolist())
        except (OSError, zipfile.BadZipFile):
            _fail("manual_webui_release_core_bound_invalid")
        if (
            archive_size != artifact.size_bytes
            or archive_size > MAX_CORE_ARCHIVE_BYTES
            or expanded_size > MAX_CORE_EXPANDED_BYTES
        ):
            _fail("manual_webui_release_core_bound_invalid")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _load_predecessor_trust(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        absolute = path.absolute()
        resolved = absolute.resolve(strict=True)
        metadata = absolute.lstat()
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("manual_webui_predecessor_trust_invalid")
    required = {
        "schema",
        "version",
        "release_id",
        "build_digest",
        "signing_key_id",
        "release_public_keys",
    }
    if (
        resolved != absolute
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 1
        or metadata.st_size > 32 * 1024
        or not isinstance(value, dict)
        or set(value) != required
    ):
        _fail("manual_webui_predecessor_trust_invalid")
    try:
        predecessor_sequence = stable_release_sequence(value["version"])
        current_sequence = stable_release_sequence(__version__)
    except (TypeError, ValueError):
        _fail("manual_webui_predecessor_trust_invalid")
    if (
        value["schema"] != PREDECESSOR_TRUST_SCHEMA
        or not isinstance(value["version"], str)
        or not is_stable_release_version(value["version"])
        or predecessor_sequence >= current_sequence
        or not isinstance(value["release_id"], str)
        or not re.fullmatch(r"release-stable-[0-9a-f]{24}", value["release_id"])
        or not isinstance(value["build_digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["build_digest"])
        or not isinstance(value["signing_key_id"], str)
        or not isinstance(value["release_public_keys"], dict)
        or not 1 <= len(value["release_public_keys"]) <= 8
    ):
        _fail("manual_webui_predecessor_trust_invalid")
    keys: dict[str, str] = {}
    for key_id, encoded in value["release_public_keys"].items():
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            _fail("manual_webui_predecessor_trust_invalid")
        if (
            not isinstance(key_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key_id)
            or len(raw) != 32
            or base64.b64encode(raw).decode("ascii") != encoded
            or not key_id.endswith(hashlib.sha256(raw).hexdigest()[:20])
        ):
            _fail("manual_webui_predecessor_trust_invalid")
        keys[key_id] = encoded
    if value["signing_key_id"] not in keys:
        _fail("manual_webui_predecessor_trust_invalid")
    return keys, {
        "version": value["version"],
        "release_id": value["release_id"],
        "build_digest": value["build_digest"],
        "signing_key_id": value["signing_key_id"],
    }


def _run(
    command: Iterable[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: int = 300,
    code: str,
) -> bytes:
    try:
        result = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail(code)
    if result.returncode != 0:
        _fail(code)
    return result.stdout


def _source_identity(source: Path, commit: str) -> None:
    if COMMIT.fullmatch(commit) is None:
        _fail("manual_webui_commit_invalid")
    head = _run(
        ("git", "rev-parse", "HEAD"),
        cwd=source,
        timeout=15,
        code="manual_webui_source_invalid",
    ).decode("ascii", errors="ignore").strip()
    tracked = _run(
        ("git", "status", "--porcelain=v1", "--untracked-files=no"),
        cwd=source,
        timeout=20,
        code="manual_webui_source_invalid",
    )
    if head != commit or tracked:
        _fail("manual_webui_source_not_exact")
    version = source / "ecorex" / "_version.py"
    if not version.is_file() or f'__version__ = "{__version__}"' not in version.read_text(
        encoding="utf-8"
    ):
        _fail("manual_webui_version_invalid")


def _base_package(path: Path, role: str) -> zipfile.ZipFile:
    name, digest = BASE_PACKAGES[role]
    if path.name != name or not path.is_file() or path.is_symlink() or _sha256(path) != digest:
        _fail(f"manual_webui_base_{role}_invalid")
    try:
        return zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        _fail(f"manual_webui_base_{role}_invalid")


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    expanded = 0
    for member in archive.infolist():
        name = member.filename.rstrip("/")
        path = PurePosixPath(name)
        mode = member.external_attr >> 16
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or name in members
            or stat.S_ISLNK(mode)
            or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
        ):
            _fail("manual_webui_archive_layout_invalid")
        expanded += member.file_size
        if len(members) >= MAX_MEMBERS or expanded > MAX_EXPANDED:
            _fail("manual_webui_archive_bound_exceeded")
        members[name] = member
    return members


def _extract_archive(path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = _safe_members(archive)
            for name, member in members.items():
                target = destination.joinpath(*PurePosixPath(name).parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                mode = member.external_attr >> 16
                target.chmod(0o755 if mode & 0o111 else 0o644)
    except ManualWebUIBuildError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _fail("manual_webui_archive_extract_failed")


def _copy_outer_member(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
    name: str,
    destination: Path,
) -> Path:
    member = members.get(name)
    if member is None or member.is_dir():
        _fail("manual_webui_base_inventory_invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, destination.open("xb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    return destination


def _load_base(
    windows_path: Path,
    macos_path: Path,
    destination: Path,
) -> tuple[ReleaseManifest, dict[str, Path], dict[str, str], bytes]:
    artifacts: dict[str, Path] = {}
    manifests: list[bytes] = []
    windows_config: dict[str, Any] | None = None
    for role, path in (("windows", windows_path), ("macos", macos_path)):
        with _base_package(path, role) as archive:
            members = _safe_members(archive)
            manifest_name = "release/release-manifest.json"
            if manifest_name not in members:
                _fail("manual_webui_base_manifest_missing")
            manifests.append(archive.read(members[manifest_name]))
            if role == "windows":
                try:
                    windows_config = json.loads(archive.read(members["bootstrap-config.json"]))
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                    _fail("manual_webui_base_bootstrap_config_invalid")
                _copy_outer_member(
                    archive,
                    members,
                    "bin/ecorex-sandbox-host.exe",
                    destination / "windows-sandbox-host.exe",
                )
            for name, member in members.items():
                if not name.startswith("release/") or member.is_dir():
                    continue
                file_name = PurePosixPath(name).name
                if file_name in {"release-manifest.json", "web-manifest.json"}:
                    continue
                target = destination / file_name
                if target.exists():
                    continue
                _copy_outer_member(archive, members, name, target)
    if len(manifests) != 2 or manifests[0] != manifests[1] or windows_config is None:
        _fail("manual_webui_base_manifest_mismatch")
    try:
        manifest = ReleaseManifest.from_json(manifests[0])
        encoded = windows_config["release_public_keys"]
        keys = {key: base64.b64decode(value, validate=True) for key, value in encoded.items()}
        publication_keys = dict(windows_config["publication_public_keys"])
        verifier = Ed25519SignatureVerifier(keys)
        verify_manifest_signature(manifest, verifier)
    except Exception:
        _fail("manual_webui_base_signature_invalid")
    if manifest.version != BASE_VERSION or manifest.release_id != BASE_RELEASE_ID:
        _fail("manual_webui_base_identity_invalid")
    required = {
        f"{kind}-{platform}-{architecture}"
        for platform, architecture in TARGETS
        for kind in ("core", "bootstrap")
    } | {
        f"capability-pack-{pack_id}-{platform}-{architecture}"
        for platform, architecture in TARGETS
        for pack_id in PACK_TOOLS
    }
    for artifact_id in required:
        try:
            artifact = manifest.artifact(artifact_id)
            path = artifacts.setdefault(artifact_id, destination / artifact.file_name)
            verify_artifact_file(path, manifest, artifact, verifier)
        except Exception:
            _fail("manual_webui_base_artifact_invalid")
    if (
        not 1 <= len(publication_keys) <= 8
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in publication_keys.items())
    ):
        _fail("manual_webui_base_publication_keys_invalid")
    return manifest, artifacts, publication_keys, (destination / "windows-sandbox-host.exe").read_bytes()


def _tracked_source_files(source: Path, directory: str) -> tuple[Path, ...]:
    payload = _run(
        ("git", "ls-files", "-z", "--", directory),
        cwd=source,
        timeout=30,
        code="manual_webui_source_inventory_invalid",
    )
    result = tuple(source / value.decode("utf-8") for value in payload.split(b"\0") if value)
    if not result or any(not path.is_file() or path.is_symlink() for path in result):
        _fail("manual_webui_source_inventory_invalid")
    return result


def _pure_runtime_overlay_files() -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []
    for distribution_name, (package_name, expected_version) in sorted(
        PURE_RUNTIME_OVERLAYS.items()
    ):
        try:
            distribution = metadata.distribution(distribution_name)
            package = Path(distribution.locate_file(package_name)).resolve(strict=True)
        except (metadata.PackageNotFoundError, OSError, RuntimeError):
            _fail("manual_webui_runtime_overlay_missing")
        if distribution.version != expected_version or not package.is_dir():
            _fail("manual_webui_runtime_overlay_invalid")
        package_files: list[tuple[str, Path]] = []
        for path in sorted(package.rglob("*")):
            relative = path.relative_to(package)
            if (
                "__pycache__" in relative.parts
                or "tests" in relative.parts
                or path.suffix.casefold()
                in {".dll", ".dylib", ".pyc", ".pyd", ".so"}
            ):
                continue
            if path.is_symlink():
                _fail("manual_webui_runtime_overlay_invalid")
            if path.is_file():
                package_files.append((f"{package_name}/{relative.as_posix()}", path))
        if f"{package_name}/__init__.py" not in {name for name, _ in package_files}:
            _fail("manual_webui_runtime_overlay_invalid")
        files.extend(package_files)
    return tuple(files)


def _install_locked_runtime_overlay(
    source: Path,
    core: Path,
    root: Path,
    *,
    platform: str,
    architecture: str,
) -> None:
    target_platform = {
        ("macos", "arm64"): "macosx_11_0_arm64",
        ("macos", "x64"): "macosx_11_0_x86_64",
        ("windows", "x64"): "win_amd64",
    }.get((platform, architecture))
    if target_platform is None:
        _fail("manual_webui_runtime_overlay_invalid")
    try:
        lock_set = load_dependency_lock_manifest(
            source / "requirements" / "locks" / "manifest.json"
        )
        profile = lock_set.profiles["runtime"]
        runtime_lock = lock_set.path.parent / profile["lock"]
        expected = active_lock_versions(runtime_lock)
    except (DependencyLockError, KeyError):
        _fail("manual_webui_runtime_overlay_invalid")
    with tempfile.TemporaryDirectory(
        dir=root, prefix=f".{platform}-{architecture}-runtime-overlay-"
    ) as raw:
        staging = Path(raw)
        _run(
            (
                sys.executable,
                "-m",
                "pip",
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--platform",
                target_platform,
                "--python-version",
                "3.11",
                "--implementation",
                "cp",
                "--target",
                str(staging),
                "-r",
                str(runtime_lock),
            ),
            cwd=source,
            timeout=900,
            code="manual_webui_runtime_overlay_install_failed",
        )
        destination = _runtime_site_packages(core, platform)
        observed: dict[str, str] = {}
        for distribution in metadata.distributions(path=[str(staging)]):
            name = re.sub(
                r"[-_.]+",
                "-",
                str(distribution.metadata.get("Name") or ""),
            ).casefold()
            if not name or name in observed:
                _fail("manual_webui_runtime_overlay_invalid")
            observed[name] = distribution.version
        if observed != expected:
            _fail("manual_webui_runtime_overlay_invalid")
        destination.mkdir(parents=True, exist_ok=True)
        for source_path in sorted(staging.iterdir(), key=lambda path: path.name.casefold()):
            if source_path.name == "bin" or source_path.name == "__pycache__":
                continue
            target = destination / source_path.name
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if source_path.is_symlink():
                _fail("manual_webui_runtime_overlay_invalid")
            if source_path.is_dir():
                shutil.copytree(
                    source_path,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "SelfTest", "*.pyc"),
                )
            else:
                shutil.copy2(source_path, target)


def _runtime_site_packages(core: Path, platform: str) -> Path:
    if platform == "windows":
        return core / "bin" / "pack-python" / "Lib" / "site-packages"
    if platform == "macos":
        return core / "bin" / "pack-python" / "lib/python3.11/site-packages"
    _fail("manual_webui_runtime_overlay_invalid")


def _install_cow_runtime_overlay(
    source: Path,
    core: Path,
    root: Path,
    *,
    platform: str,
) -> None:
    destination = _runtime_site_packages(core, platform)
    source_roots = (*COW_RUNTIME_SOURCE_ROOTS, "config.py")
    with tempfile.TemporaryDirectory(dir=root, prefix=f".{platform}-cow-overlay-") as raw:
        staging = Path(raw)
        for source_root in source_roots:
            for path in _tracked_source_files(source, source_root):
                target = staging / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        if not all((staging / source_root).exists() for source_root in source_roots):
            _fail("manual_webui_product_overlay_failed")
        destination.mkdir(parents=True, exist_ok=True)
        for source_root in source_roots:
            target = destination / source_root
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            os.replace(staging / source_root, target)


def _replace_product_imports(archive_path: Path, source: Path) -> None:
    runtime_overlays = _pure_runtime_overlay_files()
    overlay_prefixes = tuple(
        f"{package}/" for package, _ in PURE_RUNTIME_OVERLAYS.values()
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=archive_path.parent, prefix=f".{archive_path.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(archive_path) as old, zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as new:
            _safe_members(old)
            for member in old.infolist():
                if (
                    member.is_dir()
                    or member.filename.startswith("ecorex/")
                    or member.filename.startswith(overlay_prefixes)
                ):
                    continue
                new.writestr(member, old.read(member), compresslevel=9)
            for path in _tracked_source_files(source, "ecorex"):
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, FIXED_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                new.writestr(info, path.read_bytes(), compresslevel=9)
            for relative, path in runtime_overlays:
                info = zipfile.ZipInfo(relative, FIXED_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                new.writestr(info, path.read_bytes(), compresslevel=9)
        os.replace(temporary, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            members = _safe_members(archive)
        if any(
            f"{package}/__init__.py" not in members
            for package, _ in PURE_RUNTIME_OVERLAYS.values()
        ):
            _fail("manual_webui_runtime_overlay_invalid")
    except ManualWebUIBuildError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _fail("manual_webui_product_overlay_failed")
    finally:
        temporary.unlink(missing_ok=True)


def _replace_builtin_skills(core: Path, source: Path) -> None:
    destination = core / "skills"
    if destination.exists():
        shutil.rmtree(destination)
    for path in _tracked_source_files(source, "skills"):
        target = core / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _runtime_config(
    core: Path,
    *,
    platform: str,
    architecture: str,
    release_keys: Mapping[str, str],
) -> None:
    path = core / "runtime-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["identity"] = {
            "version": __version__,
            "platform": platform,
            "architecture": architecture,
        }
        value["release_public_keys"] = dict(release_keys)
        value["audit"] = {
            "endpoint": "https://dl.ecoremedia.net/api/v1/audit/records",
            "allowed_hosts": ["dl.ecoremedia.net"],
            "dispatch_seconds": 5,
            "raw_retention_days": 30,
            "aggregate_retention_days": 180,
        }
        connector_enabled = os.environ.get(
            "ECOREX_V1_FEISHU_CONNECTOR_ENABLED", "false"
        )
        if connector_enabled not in {"true", "false"}:
            _fail("manual_webui_connector_config_invalid")
        value["connectors"] = (
            {
                "endpoint": "https://dl.ecoremedia.net/api/v1/connectors",
                "allowed_hosts": ["dl.ecoremedia.net"],
                "enabled_connectors": ["feishu"],
            }
            if connector_enabled == "true"
            else None
        )
        value["capability_packs"] = list(
            required_capability_pack_projection(
                platform=platform,
                architecture=architecture,
                version=__version__,
            )
        )
        path.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            encoding="utf-8",
        )
    except (KeyError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("manual_webui_runtime_config_invalid")


def _prepare_stages(
    source: Path,
    base_artifacts: Mapping[str, Path],
    root: Path,
    release_keys: Mapping[str, str],
) -> dict[tuple[str, str], dict[str, Path]]:
    targets: dict[tuple[str, str], dict[str, Path]] = {}
    for platform, architecture in TARGETS:
        target_root = root / f"{platform}-{architecture}"
        core = target_root / "core"
        _extract_archive(base_artifacts[f"core-{platform}-{architecture}"], core)
        for generated in (core / "web", core / "web-manifest.json", core / "storage-migrations.json"):
            if generated.is_dir():
                shutil.rmtree(generated)
            else:
                generated.unlink(missing_ok=True)
        imports = tuple(core.rglob("python311.zip"))
        if len(imports) != 1:
            _fail("manual_webui_pack_python_invalid")
        _install_locked_runtime_overlay(
            source,
            core,
            target_root,
            platform=platform,
            architecture=architecture,
        )
        _install_cow_runtime_overlay(
            source,
            core,
            target_root,
            platform=platform,
        )
        _replace_product_imports(imports[0], source)
        _replace_builtin_skills(core, source)
        _runtime_config(
            core,
            platform=platform,
            architecture=architecture,
            release_keys=release_keys,
        )
        try:
            (core / "pack-python.json").write_bytes(
                build_pack_python_manifest(
                    core, platform=platform, architecture=architecture
                )
            )
            interpreter, _ = resolve_pack_python(
                core, platform=platform, architecture=architecture
            )
        except Exception:
            _fail("manual_webui_pack_python_rebind_failed")
        host_architecture = {
            "arm64": "arm64",
            "aarch64": "arm64",
            "x86_64": "x64",
            "amd64": "x64",
        }.get(host_platform.machine().casefold())
        if sys.platform == "darwin" and platform == "macos" and architecture == host_architecture:
            with tempfile.TemporaryDirectory(
                prefix="product-import-probe-", dir=target_root
            ) as probe_data:
                environment = dict(os.environ)
                environment["EMATE_DATA_DIR"] = probe_data
                _run(
                    (
                        str(interpreter),
                        "-I",
                        "-B",
                        "-c",
                        "import ecorex.bootstrap.install_local, "
                        "ecorex.integration.dependency_pack_process, lark_channel, qrcode; "
                        "from agent.tools.tool_manager import ToolManager; "
                        "from bridge.agent_initializer import AgentInitializer; "
                        "from ecorex.runtime.worker import AgentTurnWorker; "
                        "from ecorex.connectors.weixin import _qr_png_data_url; "
                        "assert ToolManager and AgentInitializer and AgentTurnWorker; "
                        "assert _qr_png_data_url('https://weixin.qq.com/q/emate').startswith('data:image/png;base64,')",
                    ),
                    cwd=core,
                    environment=environment,
                    timeout=120,
                    code="manual_webui_product_import_probe_failed",
                )
        packs: dict[str, Path] = {}
        for pack_id in sorted(PACK_TOOLS):
            pack = target_root / "packs" / pack_id
            _extract_archive(
                base_artifacts[
                    f"capability-pack-{pack_id}-{platform}-{architecture}"
                ],
                pack,
            )
            packs[pack_id] = pack
        targets[(platform, architecture)] = {"core": core, **packs}
    return targets


def _go_bootstraps(
    go: Path,
    source: Path,
    root: Path,
    *,
    release_keys: Mapping[str, str],
    publication_keys: Mapping[str, str],
    windows_helper: bytes,
    signer: Ed25519MemorySigner,
) -> dict[tuple[str, str], Path]:
    go = go.resolve(strict=True)
    version = _run(
        (str(go), "version"),
        cwd=source,
        timeout=15,
        code="manual_webui_go_invalid",
    ).decode("ascii", errors="ignore")
    if not version.startswith("go version go1.26.5 "):
        _fail("manual_webui_go_invalid")
    bootstrap_source = source / "platform-staging" / "bootstrap"
    host_environment = dict(os.environ)
    host_environment.update({"GOTOOLCHAIN": "local", "CGO_ENABLED": "0"})
    _run(
        (str(go), "test", "-mod=readonly", "./..."),
        cwd=bootstrap_source,
        environment=host_environment,
        timeout=180,
        code="manual_webui_bootstrap_tests_failed",
    )
    release_hash = hashlib.sha256(_canonical_json(dict(release_keys)).rstrip(b"\n")).hexdigest()
    publication_hash = hashlib.sha256(
        _canonical_json(dict(publication_keys)).rstrip(b"\n")
    ).hexdigest()
    public_url = "https://mvdcm.ecoremedia.net/e-mate/update/public-bootstrap-index.json"
    public_url_hash = hashlib.sha256(public_url.encode()).hexdigest()
    results: dict[tuple[str, str], Path] = {}
    for platform, architecture in TARGETS:
        target = root / f"{platform}-{architecture}" / "bootstrap"
        binary = target / "bin" / (
            "ecorex-bootstrap.exe" if platform == "windows" else "ecorex-bootstrap"
        )
        binary.parent.mkdir(parents=True)
        helper_hash = ""
        if platform == "windows":
            helper = target / "bin" / "ecorex-sandbox-host.exe"
            helper.write_bytes(windows_helper)
            helper_hash = _sha256(helper)
        linker = " ".join(
            (
                "-s",
                "-w",
                "-buildid=",
                f"-X=main.embeddedReleaseKeysSHA256={release_hash}",
                f"-X=main.embeddedSandboxHelperSHA256={helper_hash or 'none'}",
                f"-X=main.embeddedPublicIndexURLSHA256={public_url_hash}",
                f"-X=main.embeddedPublicationKeysSHA256={publication_hash}",
            )
        )
        environment = dict(host_environment)
        environment.update(
            {
                "GOOS": "windows" if platform == "windows" else "darwin",
                "GOARCH": "amd64" if architecture == "x64" else "arm64",
            }
        )
        _run(
            (
                str(go),
                "build",
                "-trimpath",
                "-buildvcs=false",
                "-mod=readonly",
                f"-ldflags={linker}",
                "-o",
                str(binary),
                ".",
            ),
            cwd=bootstrap_source,
            environment=environment,
            timeout=180,
            code="manual_webui_bootstrap_build_failed",
        )
        if platform == "macos":
            binary.chmod(0o755)
            _run(
                (
                    "/usr/bin/codesign",
                    "--force",
                    "--sign",
                    "-",
                    "--timestamp=none",
                    str(binary),
                ),
                cwd=target,
                timeout=60,
                code="manual_webui_bootstrap_adhoc_sign_failed",
            )
        installer = target / (
            "EcoreX Installer.cmd" if platform == "windows" else "EcoreX Installer.command"
        )
        if platform == "windows":
            installer.write_bytes(
                b"@echo off\r\n\"%~dp0bin\\ecorex-bootstrap.exe\" %*\r\n"
                b"exit /b %errorlevel%\r\n"
            )
        else:
            installer.write_bytes(
                b'#!/bin/sh\nBASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                b'exec "$BASE_DIR/bin/ecorex-bootstrap" "$@"\n'
            )
            installer.chmod(0o755)
        sequence = stable_release_sequence(__version__)
        minimum_payload = b"\0".join(
            (
                b"ecorex.bootstrap-minimum-stable.v1",
                str(sequence).encode("ascii"),
                __version__.encode("ascii"),
            )
        )
        config = {
            "schema_version": 1,
            "public_index_url": public_url,
            "sandbox_helper_sha256": helper_hash,
            "release_public_keys": dict(release_keys),
            "publication_public_keys": dict(publication_keys),
            "minimum_stable": {
                "sequence": sequence,
                "version": __version__,
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": signer.key_id,
                    "value": base64.b64encode(signer.sign(minimum_payload)).decode("ascii"),
                },
            },
        }
        (target / "bootstrap-config.json").write_bytes(_canonical_json(config))
        results[(platform, architecture)] = target
    host = results[("macos", "arm64")] / "bin" / "ecorex-bootstrap"
    probe = json.loads(
        _run(
            (str(host), "--self-test"),
            cwd=results[("macos", "arm64")],
            timeout=15,
            code="manual_webui_bootstrap_self_test_failed",
        )
    )
    if probe.get("platform") != "macos" or probe.get("architecture") != "arm64":
        _fail("manual_webui_bootstrap_self_test_failed")
    return results


def _sources() -> tuple[ReleaseSource, ...]:
    github = (
        "https://github.com/zyfjacksonchen-source/"
        f"EcoreX/releases/download/v{__version__}"
    )
    return (
        ReleaseSource(
            "github-cn",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            f"https://gh-proxy.com/{github}",
        ),
        ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, github),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://mvdcm.ecoremedia.net/e-mate/update",
        ),
    )


def _core_executable_paths(platform: str) -> tuple[str, ...]:
    if platform == "windows":
        return ("bin/ecorex.exe",)
    return ("bin/ecorex", "bin/pack-python/bin/python3")


def _build_release(
    stages: Mapping[tuple[str, str], Mapping[str, Path]],
    bootstraps: Mapping[tuple[str, str], Path],
    web_dist: Path,
    destination: Path,
    signer: Ed25519MemorySigner,
    generated_at: str,
    source: Path,
) -> Any:
    artifacts: list[ArtifactBuildInput] = []
    for platform, architecture in TARGETS:
        target = stages[(platform, architecture)]
        artifacts.extend(
            (
                ArtifactBuildInput(
                    target["core"],
                    ArtifactKind.CORE,
                    platform,
                    architecture,
                    executable_paths=_core_executable_paths(platform),
                    product_runtime=True,
                ),
                ArtifactBuildInput(
                    bootstraps[(platform, architecture)],
                    ArtifactKind.BOOTSTRAP,
                    platform,
                    architecture,
                    executable_paths=(
                        "bin/ecorex-bootstrap.exe"
                        if platform == "windows"
                        else "bin/ecorex-bootstrap",
                    ),
                ),
            )
        )
        for pack_id in sorted(PACK_TOOLS):
            artifacts.append(
                ArtifactBuildInput(
                    target[pack_id],
                    ArtifactKind.CAPABILITY_PACK,
                    platform,
                    architecture,
                    executable_paths=(),
                    pack_id=pack_id,
                    pack_tool_ids=tuple(PACK_TOOLS[pack_id]),
                    pack_service_ids=tuple(PACK_SERVICES[pack_id]),
                    runtime_api_version="1.0.0",
                )
            )
    return ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at=generated_at,
            sources=_sources(),
            artifacts=tuple(artifacts),
            web_bundle=WebBundleBuildInput(web_dist),
            dependency_lock_sha256=load_dependency_lock_manifest(
                source / "requirements" / "locks" / "manifest.json"
            ).sha256,
        ),
        destination,
    )


def _write_zip(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with zipfile.ZipFile(
            temporary,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, FIXED_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                executable = relative.endswith((".exe", ".cmd", ".command"))
                info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                with path.open("rb") as input_file, archive.open(info, "w") as output:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _release_contract(manifest: ReleaseManifest, *, platform: str, entry: str) -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "product": "e-Mate",
            "version": __version__,
            "platform": platform,
            "install_entry": entry,
            "ui_kind": "browser-webui",
            "os_application_signature": False,
            "release_id": manifest.release_id,
            "build_digest": manifest.build_digest,
        }
    )


def _build_outer_packages(built: Any, output: Path) -> tuple[Path, Path]:
    manifest = built.manifest
    with tempfile.TemporaryDirectory(prefix="emate-v1-webui-", dir=output) as temporary:
        root = Path(temporary)
        windows = root / "windows"
        macos = root / "macos"
        for package_root, targets in (
            (windows, {("windows", "x64")}),
            (macos, {("macos", "arm64"), ("macos", "x64")}),
        ):
            release = package_root / "release"
            release.mkdir(parents=True)
            shutil.copy2(built.manifest_path, release / "release-manifest.json")
            for artifact in manifest.artifacts:
                if (
                    (artifact.platform, artifact.architecture) in targets
                    or artifact.artifact_id == "web-manifest"
                ):
                    shutil.copy2(
                        built.artifact_paths[artifact.artifact_id],
                        release / artifact.file_name,
                    )
        bootstrap_artifact = manifest.artifact("bootstrap-windows-x64")
        _extract_archive(
            built.artifact_paths[bootstrap_artifact.artifact_id], windows
        )
        (windows / "EcoreX Installer.cmd").unlink()
        (windows / "Install EcoreX WebUI.cmd").write_bytes(
            b"@echo off\r\n\"%~dp0bin\\ecorex-bootstrap.exe\" --local-release "
            b"\"%~dp0release\"\r\nexit /b %errorlevel%\r\n"
        )
        (windows / "README.txt").write_text(
            "Run Install EcoreX WebUI.cmd. This package serves the e-Mate WebUI "
            "in your browser and contains no native desktop UI.\n",
            encoding="utf-8",
        )
        (windows / "release.json").write_bytes(
            _release_contract(
                manifest, platform="windows-x64", entry="Install EcoreX WebUI.cmd"
            )
        )
        (macos / "Install EcoreX WebUI.command").write_text(
            '#!/bin/sh\nset -eu\nBASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'case "$(uname -m)" in arm64) TARGET=arm64 ;; x86_64) TARGET=x64 ;; '
            '*) echo \'e-Mate 不支持当前架构\' >&2; exit 78 ;; esac\n'
            'DEST=${TMPDIR:-/tmp}/emate-bootstrap-$TARGET-$$\nmkdir -m 700 "$DEST"\n'
            "trap 'rm -rf \"$DEST\"' EXIT HUP INT TERM\n"
            'ARCHIVE=$(find "$BASE_DIR/release" -maxdepth 1 -type f -name '
            f'"ecorex-bootstrap-macos-$TARGET-{__version__}.zip" -print)\n'
            'test -n "$ARCHIVE" && test "$(printf \'%s\\n\' "$ARCHIVE" | wc -l | tr -d \' \')" = 1\n'
            '/usr/bin/ditto -x -k "$ARCHIVE" "$DEST"\n'
            'exec "$DEST/bin/ecorex-bootstrap" --local-release "$BASE_DIR/release" "$@"\n',
            encoding="utf-8",
        )
        (macos / "Install EcoreX WebUI.command").chmod(0o755)
        (macos / "README.txt").write_text(
            "Run Install EcoreX WebUI.command. This terminal-distributed package "
            "serves the e-Mate WebUI in your browser and contains no app bundle.\n",
            encoding="utf-8",
        )
        (macos / "release.json").write_bytes(
            _release_contract(
                manifest,
                platform="macos-universal",
                entry="Install EcoreX WebUI.command",
            )
        )
        windows_path = output / WINDOWS_PACKAGE
        macos_path = output / MACOS_PACKAGE
        _write_zip(windows, windows_path)
        _write_zip(macos, macos_path)
    return windows_path, macos_path


def _verify_outer(
    package: Path,
    *,
    targets: set[tuple[str, str]],
    release_keys: Mapping[str, bytes],
) -> dict[str, Any]:
    verifier = Ed25519SignatureVerifier(dict(release_keys))
    try:
        with zipfile.ZipFile(package) as archive:
            members = _safe_members(archive)
            if archive.testzip() is not None:
                _fail("manual_webui_package_corrupt")
            forbidden = (".app/", ".dmg", ".pkg")
            if any(any(marker in name.casefold() for marker in forbidden) for name in members):
                _fail("manual_webui_native_shell_forbidden")
            manifest_payload = archive.read(members["release/release-manifest.json"])
            manifest = ReleaseManifest.from_json(manifest_payload)
            verify_manifest_signature(manifest, verifier)
            included = tuple(
                item
                for item in manifest.artifacts
                if (item.platform, item.architecture) in targets
                or item.artifact_id == "web-manifest"
            )
            with tempfile.TemporaryDirectory(prefix="emate-v1-verify-") as temporary:
                root = Path(temporary)
                paths: dict[str, Path] = {}
                for artifact in included:
                    name = f"release/{artifact.file_name}"
                    if name not in members:
                        _fail("manual_webui_package_inventory_invalid")
                    target = root / artifact.file_name
                    _copy_outer_member(archive, members, name, target)
                    verify_artifact_file(target, manifest, artifact, verifier)
                    paths[artifact.artifact_id] = target
                web = paths["web-manifest"]
                contracts = []
                for platform, architecture in sorted(targets):
                    contracts.append(
                        _verify_webui_contract(
                            paths[f"core-{platform}-{architecture}"],
                            web,
                            manifest=manifest,
                            verifier=verifier,
                            expected_platform=platform,
                            expected_architecture=architecture,
                        )
                    )
                if len({value["bundle_sha256"] for value in contracts}) != 1:
                    _fail("manual_webui_web_bundle_mismatch")
    except ManualWebUIBuildError:
        raise
    except Exception:
        _fail("manual_webui_package_verification_failed")
    return {
        "file_name": package.name,
        "size_bytes": package.stat().st_size,
        "sha256": _sha256(package),
        "targets": [f"{platform}-{architecture}" for platform, architecture in sorted(targets)],
        "os_application_signature": False,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    if not is_stable_release_version(__version__):
        _fail("manual_webui_version_invalid")
    source = args.source.resolve(strict=True)
    web_dist = args.web_dist.resolve(strict=True)
    _source_identity(source, args.commit_sha)
    if web_dist.parent != source / "desktop" or not (web_dist / "index.html").is_file():
        _fail("manual_webui_web_dist_invalid")
    output = args.output.resolve()
    if output.exists():
        _fail("manual_webui_output_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    generated_at = args.generated_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = f"ecorex-webui-release-{hashlib.sha256(public).hexdigest()[:20]}"
    signer = Ed25519MemorySigner(key_id, private)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    staged = temporary / "verified-output"
    staged.mkdir()
    try:
        base_manifest, base_artifacts, publication_keys, windows_helper = _load_base(
            args.base_windows.resolve(strict=True),
            args.base_macos.resolve(strict=True),
            temporary / "base",
        )
        existing_keys: dict[str, str] = {}
        for platform, architecture in TARGETS:
            artifact = base_manifest.artifact(f"core-{platform}-{architecture}")
            core = temporary / "config-core"
            _extract_archive(base_artifacts[artifact.artifact_id], core)
            config = json.loads((core / "runtime-config.json").read_text(encoding="utf-8"))
            candidate = dict(config["release_public_keys"])
            shutil.rmtree(core)
            if existing_keys and candidate != existing_keys:
                _fail("manual_webui_base_trust_mismatch")
            existing_keys = candidate
        predecessor_keys, predecessor = _load_predecessor_trust(
            args.predecessor_trust
        )
        release_keys = dict(existing_keys)
        for predecessor_key_id, encoded in predecessor_keys.items():
            current = release_keys.get(predecessor_key_id)
            if current is not None and current != encoded:
                _fail("manual_webui_predecessor_trust_conflict")
            release_keys[predecessor_key_id] = encoded
        release_keys[key_id] = base64.b64encode(public).decode("ascii")
        if len(release_keys) > 8:
            _fail("manual_webui_release_trust_too_large")
        stages = _prepare_stages(
            source,
            base_artifacts,
            temporary / "stages",
            release_keys,
        )
        bootstraps = _go_bootstraps(
            args.go,
            source,
            temporary / "stages",
            release_keys=release_keys,
            publication_keys=publication_keys,
            windows_helper=windows_helper,
            signer=signer,
        )
        built = _build_release(
            stages,
            bootstraps,
            web_dist,
            staged / "release",
            signer,
            generated_at,
            source,
        )
        _verify_release_evidence_bounds(built.metadata_path, built.sbom_path)
        _verify_release_core_bounds(built)
        verifier = Ed25519SignatureVerifier({key_id: public})
        verify_manifest_signature(built.manifest, verifier)
        for artifact in built.manifest.artifacts:
            verify_artifact_file(
                built.artifact_paths[artifact.artifact_id],
                built.manifest,
                artifact,
                verifier,
            )
        windows, macos = _build_outer_packages(built, staged)
        package_evidence = [
            _verify_outer(
                windows,
                targets={("windows", "x64")},
                release_keys={key_id: public},
            ),
            _verify_outer(
                macos,
                targets={("macos", "arm64"), ("macos", "x64")},
                release_keys={key_id: public},
            ),
        ]
        manifest_sha256 = _sha256(built.manifest_path)
        shutil.copy2(built.manifest_path, staged / "release-manifest.json")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "verified",
            "version": __version__,
            "generated_at": generated_at,
            "source_commit": args.commit_sha,
            "release_id": built.manifest.release_id,
            "build_digest": built.manifest.build_digest,
            "manifest_sha256": manifest_sha256,
            "base": {
                "version": BASE_VERSION,
                "release_id": BASE_RELEASE_ID,
                "packages": {
                    role: {"file_name": value[0], "sha256": value[1]}
                    for role, value in BASE_PACKAGES.items()
                },
            },
            "predecessor": {
                **predecessor,
                "trusted_key_ids": sorted(predecessor_keys),
            },
            "signing": {
                "inner_integrity": "ed25519",
                "key_id": key_id,
                "public_key_sha256": hashlib.sha256(public).hexdigest(),
                "private_key_persisted": False,
                "os_application_signature": False,
                "macos_signature_mode": "adhoc-code-directory",
                "developer_id": False,
                "notarized": False,
            },
            "artifacts": package_evidence,
        }
        (staged / "manual-webui-build-receipt.json").write_bytes(
            _canonical_json(receipt)
        )
        os.replace(staged, output)
        return receipt
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--web-dist", required=True, type=Path)
    parser.add_argument("--base-windows", required=True, type=Path)
    parser.add_argument("--base-macos", required=True, type=Path)
    parser.add_argument("--predecessor-trust", required=True, type=Path)
    parser.add_argument("--go", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--generated-at")
    return parser


def main() -> int:
    try:
        result = build(_parser().parse_args())
    except ManualWebUIBuildError as error:
        print(json.dumps({"ok": False, "code": str(error)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "code": "manual_webui_build_failed"}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": result["status"],
                "version": result["version"],
                "release_id": result["release_id"],
                "build_digest": result["build_digest"],
                "manifest_sha256": result["manifest_sha256"],
                "artifacts": result["artifacts"],
                "private_key_persisted": False,
                "developer_id": False,
                "notarized": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
